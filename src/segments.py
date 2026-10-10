"""決算短信のセグメント情報(XBRL の …sg…-ixbrl.htm)から、セグメント別の売上高・利益を data/segments.csv にためる。

表を読むのではなく XBRL の数値(セグメントごとのメンバー × 当期/前年同期)をそのまま使う。
セグメント名は短信の XBRL に入っている日本語ラベル(…-lab.xml)。金額は円・期首からの累計。
"""
import io
import json
import logging
import re
import zipfile
from pathlib import Path

import pandas as pd

from . import disclosures, earnings
from .config import DATA_DIR
from .datasources import tdnet
from .datasources.http import RateLimitedSession

log = logging.getLogger(__name__)

COLUMNS = ["disclosure_id", "code", "disclosed_at", "n_q", "period_end", "status", "data"]

# 売上(セグメント間を含む「計」)と利益。上から順に、あるものを使う
SALES = ["NetSales", "OperatingRevenue1", "NetSalesIFRS", "RevenueIFRS", "RevenueFromContractsWithCustomers",
         "Revenue", "OperatingRevenueIFRS"]
SALES_PARTS = [("RevenuesFromExternalCustomers", "TransactionsWithOtherSegments"),
               ("SalesToExternalCustomersIFRS", "IntersegmentSalesIFRS"),
               ("RevenueFromExternalCustomersIFRS", "IntersegmentRevenueIFRS")]
PROFIT = ["OperatingIncome", "OperatingProfitLossIFRS", "SegmentProfitLossIFRS", "BusinessProfitLossIFRS",
          "OrdinaryIncome", "ProfitLossBeforeTaxIFRS"]
# 中間期は「InterimDuration」(Current が付かない)
_CTX = re.compile(r"^(Current|Prior1)?(YTD|Interim|Year)Duration_(?:NonConsolidatedMember_)?(.+Member)$")
STANDARD = {"OperatingSegmentsNotIncludedInReportableSegmentsAndOtherRevenueGeneratingBusinessActivitiesMember": "その他",
            "OtherReportableSegmentsMember": "その他"}
TOTAL = "EntityTotalMember"
SKIP = {"ReportableSegmentsMember", "TotalOfReportableSegmentsAndOthersMember", "ReconcilingItemsMember"}


def path(data_dir: Path = DATA_DIR) -> Path:
    return data_dir / "segments.csv"


def load(data_dir: Path = DATA_DIR) -> pd.DataFrame:
    p = path(data_dir)
    if not p.exists():
        return pd.DataFrame(columns=COLUMNS)
    return pd.read_csv(p, dtype=str, keep_default_na=False).reindex(columns=COLUMNS, fill_value="")


def by_code(data_dir: Path = DATA_DIR) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    df = load(data_dir)
    for r in df[df["status"] == "ok"].sort_values("disclosed_at").to_dict("records"):
        r["data"] = json.loads(r["data"])
        out.setdefault(r["code"], []).append(r)
    return out


_EL = re.compile(r"<link:(loc|labelArc|label)\b([^>]*?)/?>(?:([^<]*)</link:label>)?", re.S)
_AT = re.compile(r'([\w:]+)="([^"]*)"')


def _labels(z: zipfile.ZipFile) -> dict[str, str]:
    """独自のセグメントメンバー → 日本語名(contextRef と同じ「接頭辞+名前」の形のキー)。
    ラベルの付け方は会社ごとに違うので、loc(要素) → labelArc → label(文字) の順にたどる。"""
    out = {}
    for n in z.namelist():
        if not n.endswith("-lab.xml"):
            continue
        s = z.read(n).decode("utf-8", errors="replace")
        locs, arcs, texts = {}, {}, {}
        for kind, attrs, text in _EL.findall(s):
            a = dict(_AT.findall(attrs))
            if kind == "loc":
                locs[a.get("xlink:label", "")] = a.get("xlink:href", "").split("#")[-1]
            elif kind == "labelArc":
                arcs.setdefault(a.get("xlink:from", ""), []).append(a.get("xlink:to", ""))
            elif a.get("xlink:role", "").endswith("/role/label") and text.strip():
                texts[a.get("xlink:label", "")] = text.strip()
        for loc, el_id in locs.items():
            name = next((texts[t] for t in arcs.get(loc, []) if t in texts), None)
            if name:
                out[el_id.replace("_", "", 1)] = name
    return out


def _pick(f: dict, names: list[str]) -> str | None:
    return next((n for n in names if n in f), None)


def parse_zip(zip_bytes: bytes) -> dict | None:
    """{segments: [{name, sales, sales_prior, op, op_prior}], total: {...}}(円・累計)。セグメントが2つ未満なら None。"""
    try:
        z = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile:
        return None
    files = [n for n in z.namelist() if re.search(r"/\d{7}-\w\wsg\d\d-.*ixbrl\.htm$", n)]
    if not files:
        return None
    facts = tdnet._scaled_facts(z.read(files[0]).decode("utf-8", errors="replace"))
    labels = _labels(z)
    by_member: dict[str, dict[str, dict[str, float]]] = {}       # member -> cur/prior -> name -> 値
    order = []
    for (name, ctx), v in facts.items():
        m = _CTX.match(ctx)
        if not m:
            continue
        member, when = m.group(3), "prior" if m.group(1) == "Prior1" else "cur"
        if member not in by_member:
            by_member[member] = {"cur": {}, "prior": {}}
            order.append(member)
        by_member[member][when][name] = v

    def values(f):
        s = _pick(f, SALES)
        sales = f[s] if s else next((f[a] + f.get(b, 0) for a, b in SALES_PARTS if a in f), None)
        p = _pick(f, PROFIT)
        return sales, (f[p] if p else None)

    segs = []
    for member in order:
        if member in SKIP or member == TOTAL:
            continue
        name = STANDARD.get(member) or labels.get(member)
        if not name:
            continue
        (s, p), (sp, pp) = values(by_member[member]["cur"]), values(by_member[member]["prior"])
        if s is None and p is None:
            continue
        segs.append({"name": name, "sales": s, "sales_prior": sp, "op": p, "op_prior": pp})
    if len(segs) < 2:
        return None
    total = None
    if TOTAL in by_member:
        (s, p), (sp, pp) = values(by_member[TOTAL]["cur"]), values(by_member[TOTAL]["prior"])
        total = {"name": "合計", "sales": s, "sales_prior": sp, "op": p, "op_prior": pp}
    return {"segments": segs, "total": total}


def update(cfg: dict, data_dir: Path = DATA_DIR, session: RateLimitedSession | None = None,
           limit: int = 300) -> int:
    """まだ読んでいない決算短信のセグメント情報を読む(セグメントが無い短信も status=none で記録)。"""
    disc = disclosures.load(data_dir)
    have = set(load(data_dir)["disclosure_id"])
    todo = disc[(disc["kind"] == "earnings_report") & (disc["is_correction"] != "True") & (disc["xbrl_url"] != "")
                & ~disc["disclosure_id"].isin(have)].sort_values("disclosed_at").head(limit)
    if todo.empty:
        return 0
    arch = {a["disclosure_id"]: a for a in earnings.load_archive(data_dir).to_dict("records")}
    session = session or RateLimitedSession.from_config(cfg)
    rows = []
    for r in todo.itertuples():
        try:
            content = tdnet.get_zip(session, r.xbrl_url)    # 決算の数値を読んだときの zip を使い回す
        except Exception as e:
            log.warning("セグメント: XBRL取得失敗 %s: %s", r.code, e)
            continue
        if content is None:
            continue
        try:
            x = parse_zip(content)
        except Exception as e:
            log.warning("セグメント: 読み取り失敗 %s: %s", r.code, e)
            x = None
        a = arch.get(r.disclosure_id) or {}
        rows.append({"disclosure_id": r.disclosure_id, "code": r.code, "disclosed_at": r.disclosed_at,
                     "n_q": a.get("n_q", ""), "period_end": a.get("period_end", ""), "status": "ok" if x else "none",
                     "data": json.dumps(x, ensure_ascii=False) if x else ""})
    if rows:
        df = pd.concat([load(data_dir), pd.DataFrame(rows).reindex(columns=COLUMNS)])
        df.sort_values(["disclosed_at", "disclosure_id"], ascending=False).to_csv(path(data_dir), index=False,
                                                                                encoding="utf-8")
    log.info("segments: %d (ok %d)", len(rows), sum(r["status"] == "ok" for r in rows))
    return len(rows)
