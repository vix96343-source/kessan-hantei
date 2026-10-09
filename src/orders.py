"""決算短信の本文(TDnet XBRL の qualitative.htm)にある受注高・受注残高の表を読み、data/orders.csv にためる。

短信の「生産、受注及び販売の状況」などの表をそのまま読む(文章からは取らない)。表の形は会社ごとに違うので、
列・行の見出しの言葉(受注高/受注残/前年同期比/前第○四半期…)から意味を決める。
前年同期比は「前年の何%か(256.4)」と「増減率(+156.4)」の2通りの書き方があるため、
符号・見出し・セグメントの合算が合計と合うかで見分け、増減率(%)にそろえて持つ。
金額はすべて円。QoQ は site.py で前回の短信の値と比べて出す。
"""
import io
import json
import logging
import re
import zipfile
from pathlib import Path

import pandas as pd
from bs4 import BeautifulSoup

from . import disclosures, earnings
from .config import DATA_DIR
from .datasources.http import RateLimitedSession

log = logging.getLogger(__name__)

COLUMNS = ["disclosure_id", "code", "disclosed_at", "n_q", "period_end", "status", "data"]
METRICS = ("orders", "backlog")

_TABLE_HINT = re.compile(r"受注(高|残|額|総額|金額|工事高|実績)|手持受注|繰越工事高")
_NUM = re.compile(r"^[△▲\-－−+＋]?[\d,，]+(\.\d+)?[%％]?$")
_UNITS = {"百万円": 1e6, "千円": 1e3, "億円": 1e8, "円": 1.0}
_UNIT_RE = re.compile(r"(百万円|千円|億円)")
_TOTAL = re.compile(r"^(合計|総合計|総計|受注高合計|全社合計|連結合計)$")
_SUBTOTAL = re.compile(r"計$")


def path(data_dir: Path = DATA_DIR) -> Path:
    return data_dir / "orders.csv"


def load(data_dir: Path = DATA_DIR) -> pd.DataFrame:
    p = path(data_dir)
    if not p.exists():
        return pd.DataFrame(columns=COLUMNS)
    return pd.read_csv(p, dtype=str, keep_default_na=False).reindex(columns=COLUMNS, fill_value="")


def by_code(data_dir: Path = DATA_DIR) -> dict[str, list[dict]]:
    """code → 受注の取れた短信(古い順)。data は dict に戻して入れる。"""
    out: dict[str, list[dict]] = {}
    df = load(data_dir)
    for r in df[df["status"] == "ok"].sort_values("disclosed_at").to_dict("records"):
        r["data"] = json.loads(r["data"])
        out.setdefault(r["code"], []).append(r)
    return out


# ---------------------------------------------------------------- 表の読み取り

def _clean(t: str) -> str:
    return re.sub(r"\s+", " ", t.replace("　", " ")).strip()


def _num(text: str) -> float | None:
    """'1,234' '△5.2' '＋101.1' '219.2％' '50,163 (45,839)' → 数値。'－' や文字は None。"""
    t = re.sub(r"\s", "", text)
    t = re.sub(r"[（(][^)）]*[)）]$", "", t)          # 後ろの括弧書き(単体の内数など)は捨てる
    if not t or not _NUM.match(t):
        return None
    v = float(re.sub(r"[^\d.]", "", t))
    return -v if t[0] in "△▲-－−" else v


def _signed(text: str) -> bool:
    return bool(re.match(r"^\s*[△▲\-－−+＋]\s*\d", text))


def _grid(table) -> list[list[str]]:
    """rowspan / colspan を展開した表(セルの文字列の2次元配列)"""
    grid, spans = [], {}                               # spans: 列 → [残り行数, 文字]
    for tr in table.find_all("tr"):
        if tr.find_parent("table") is not table:
            continue
        cells = tr.find_all(["td", "th"], recursive=False)
        row, ci, col = [], 0, 0
        while ci < len(cells) or any(k >= col for k in spans):
            if col in spans:
                spans[col][0] -= 1
                row.append(spans[col][1])
                if spans[col][0] <= 0:
                    del spans[col]
                col += 1
                continue
            if ci >= len(cells):
                row.append("")
                col += 1
                continue
            c = cells[ci]
            ci += 1
            text = _clean(c.get_text(" ", strip=True))
            cs = int(re.sub(r"\D", "", c.get("colspan", "1")) or 1)
            rs = int(re.sub(r"\D", "", c.get("rowspan", "1")) or 1)
            for _ in range(cs):
                row.append(text)
                if rs > 1:
                    spans[col] = [rs - 1, text]
                col += 1
        grid.append(row)
    width = max((len(r) for r in grid), default=0)
    return [r + [""] * (width - len(r)) for r in grid]


def _metric(label: str) -> str | None:
    l = re.sub(r"\s", "", label)
    if re.search(r"件|損失|前期繰越", l):
        return None
    if re.search(r"受注残|手持受注|次期繰越|繰越工事高", l):
        return "backlog"
    if re.search(r"受注(高|額|総額|金額|工事高|実績)|受注$|受注[(（]", l):
        return "orders"
    return None


def _col_kind(label: str) -> str | None:
    """値の列の種類: value(金額) / pct(前年同期比・増減率) / None(構成比・増減額など使わない列)"""
    l = re.sub(r"\s", "", label)
    if "構成比" in l:
        return None
    if "率" in l or re.search(r"比(?!較)", l):
        return "pct"
    if re.search(r"増減|対前|比較|差", l):
        return None
    return "value"


def _period(label: str) -> str:
    l = re.sub(r"\s", "", label)
    if re.search(r"前(事業|連結会計)年度|前期末|参考", l):
        return "fy"                                    # 前期(通期)。四半期の表なら前期末、通期の表なら前年
    if re.search(r"前(第|中間|年|期|四半期|連結|事業|会計)", l):
        return "prior"
    return "cur"


def _unit(table, header_text: str) -> float | None:
    m = _UNIT_RE.search(header_text)
    if m:
        return _UNITS[m.group(1)]
    before = ""
    for s in table.find_all_previous(string=True, limit=8):
        before = str(s) + before
    m = _UNIT_RE.search(before[-200:])
    return _UNITS[m.group(1)] if m else None


def parse_table(table) -> tuple[list[dict], float | None]:
    """1つの表から事実のリスト [{seg, metric, period, kind, value, raw}] と単位(円/単位)を返す。"""
    grid = _grid(table)
    if not grid:
        return [], None
    is_data = [any(_num(c) is not None for c in row) for row in grid]
    if not any(is_data):
        return [], None
    first = is_data.index(True)
    header = grid[:first]
    width = len(grid[0])
    numeric_cols = {j for i, row in enumerate(grid) if is_data[i] for j, c in enumerate(row) if _num(c) is not None}
    label_cols = [j for j in range(width) if j not in numeric_cols]

    def col_label(j):
        parts = []
        for row in header:
            if row[j] and (not parts or parts[-1] != row[j]):
                parts.append(row[j])
        return " ".join(parts)

    labels = {j: col_label(j) for j in range(width)}
    all_header = " ".join(labels.values())
    annual = bool(re.search(r"当(事業|連結会計)年度|当期", re.sub(r"\s", "", all_header))) and \
        not re.search(r"当(第|中間)", re.sub(r"\s", "", all_header))
    cols = {}
    last_metric = None
    for j in sorted(numeric_cols):
        kind = _col_kind(labels[j])
        metric = _metric(labels[j])
        if kind == "value" and metric:
            last_metric = metric
        elif kind == "pct" and metric is None:
            metric = last_metric                       # 「受注高 | 前年同期比 | 受注残高 | 前年同期比」
        period = _period(labels[j]) if kind == "value" else "cur"
        if period == "fy" and annual:
            period = "prior"
        cols[j] = {"kind": kind, "metric": metric, "period": period,
                   "growth_label": "増減率" in re.sub(r"\s", "", labels[j])}

    facts = []
    for i, row in enumerate(grid):
        if not is_data[i]:
            continue
        names = []
        for j in label_cols:
            if row[j] and (not names or names[-1] != row[j]):
                names.append(row[j])
        if not names or re.match(r"^[（(]", names[-1]):
            continue                                   # 「(非住宅物件等含む)」のような内数の行
        row_metric = next((m for m in (_metric(n) for n in names) if m), None)
        seg = names[-1]
        if _metric(seg) and len(names) == 1:
            seg = "合計"                               # 「受注高 | 50,163 | 59,267 …」のように行見出しが項目名
        for j, c in cols.items():
            if c["kind"] is None:
                continue
            metric = c["metric"] or row_metric
            v = _num(row[j])
            if metric is None or v is None:
                continue
            facts.append({"seg": seg, "metric": metric, "period": c["period"], "kind": c["kind"],
                          "value": v, "signed": _signed(row[j]), "growth_label": c["growth_label"]})
    return facts, _unit(table, all_header)


def _seg_key(name: str) -> str:
    return re.sub(r"\s", "", name)


def _assemble(facts: list[dict], unit: float) -> dict | None:
    """事実のリスト → {segments: [...], total: {...}}(円・増減率%)"""
    segs: dict[str, dict] = {}
    order = []
    for f in facts:
        k = _seg_key(f["seg"])
        if k not in segs:
            segs[k] = {"name": f["seg"]}
            order.append(k)
        s = segs[k]
        if f["kind"] == "value":
            s.setdefault(f"{f['metric']}_{f['period']}", f["value"] * unit)
        else:
            s.setdefault(f"{f['metric']}_raw", f["value"])
            s[f"{f['metric']}_signed"] = s.get(f"{f['metric']}_signed", False) or f["signed"]
            s[f"{f['metric']}_growth_label"] = f["growth_label"]

    totals = [k for k in order if _TOTAL.match(k)]
    subtotals = [k for k in order if k not in totals and _SUBTOTAL.search(k)]
    leaves = [k for k in order if k not in totals and k not in subtotals]
    if not totals:
        totals = [k for k in subtotals if k == "計"][-1:]
        subtotals = [k for k in subtotals if k not in totals]
    if totals:
        total = segs[totals[-1]]
    elif len(leaves) == 1:
        total = segs[leaves[0]]
    elif leaves:                                       # 合計の行が無ければ足し上げる
        total = {"name": "合計"}
        for m in METRICS:
            for p in ("cur", "prior", "fy"):
                vals = [segs[k].get(f"{m}_{p}") for k in leaves]
                if vals and all(v is not None for v in vals):
                    total[f"{m}_{p}"] = sum(vals)
    else:
        return None

    for m in METRICS:
        style = _pct_style([segs[k] for k in leaves], total, m)
        for s in [segs[k] for k in leaves] + [total]:
            s[f"{m}_yoy"] = _yoy(s, m, style)

    def out(s):
        return {"name": s["name"], **{f"{m}{suf}": s.get(f"{m}_{p}") for m in METRICS
                                       for suf, p in (("", "cur"), ("_prior", "prior"), ("_fy", "fy"))},
                **{f"{m}_yoy": s.get(f"{m}_yoy") for m in METRICS}}

    t = out(total)
    if t["orders"] is None and t["backlog"] is None:
        return None
    segments = [out(segs[k]) for k in leaves] if len(leaves) > 1 else []
    return {"segments": segments, "total": t}


def _pct_style(leaves: list[dict], total: dict, m: str) -> str:
    """前年同期比の書き方: growth(増減率) / ratio(前年=100)"""
    rows = [s for s in leaves + [total] if s.get(f"{m}_raw") is not None]
    if not rows:
        return "growth"
    if any(s.get(f"{m}_signed") or s.get(f"{m}_growth_label") for s in rows):
        return "growth"
    if any(s[f"{m}_raw"] < 0 for s in rows):
        return "growth"
    # セグメントの前年値の合計が、合計の前年値と合うほうを採る
    lv = [s for s in leaves if s.get(f"{m}_raw") is not None and s.get(f"{m}_cur") is not None]
    if len(lv) >= 2 and len(lv) == len(leaves) and total.get(f"{m}_raw") is not None and total.get(f"{m}_cur"):
        def err(base):
            if any(base + s[f"{m}_raw"] <= 0 for s in lv + [total]):
                return float("inf")
            seg_sum = sum(s[f"{m}_cur"] / ((base + s[f"{m}_raw"]) / 100) for s in lv)
            tot = total[f"{m}_cur"] / ((base + total[f"{m}_raw"]) / 100)
            return abs(seg_sum - tot) / abs(tot)
        e_ratio, e_growth = err(0), err(100)
        if e_ratio < 0.003 and e_growth > 0.01:
            return "ratio"
        if e_growth < 0.003 and e_ratio > 0.01:
            return "growth"
    return "ratio" if min(s[f"{m}_raw"] for s in rows) >= 30 else "growth"


def _yoy(s: dict, m: str, style: str) -> float | None:
    cur, prior = s.get(f"{m}_cur"), s.get(f"{m}_prior")
    if cur is not None and prior:
        return round((cur - prior) / abs(prior) * 100, 1)
    raw = s.get(f"{m}_raw")
    if raw is None:
        return None
    return round(raw - 100, 1) if style == "ratio" else raw


def parse_qualitative(html: str, cum_sales: float | None = None) -> dict | None:
    """qualitative.htm 全体から受注の表を探して読む。無ければ None。"""
    soup = BeautifulSoup(html, "html.parser")
    facts, unit = [], None
    for t in soup.find_all("table"):
        text = t.get_text(" ", strip=True)
        if "……" in text or len(text) > 6000 or not _TABLE_HINT.search(re.sub(r"\s", "", text)):
            continue
        f, u = parse_table(t)
        if f:
            facts += f
            unit = unit or u
    if not facts:
        return None
    unit = unit or _guess_unit(facts, cum_sales)
    return _assemble(facts, unit)


def _guess_unit(facts: list[dict], cum_sales: float | None) -> float:
    """単位が書いていない表: 売上高と同じくらいの桁になる単位を選ぶ(わからなければ百万円)"""
    vals = [f["value"] for f in facts if f["kind"] == "value" and f["metric"] == "orders" and f["period"] == "cur"]
    if not cum_sales or not vals:
        return 1e6
    v = max(vals)
    for u in (1e6, 1e3, 1e8, 1.0):
        if 0.05 <= v * u / cum_sales <= 20:
            return u
    return 1e6


def parse_zip(zip_bytes: bytes, cum_sales: float | None = None) -> dict | None:
    try:
        z = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile:
        return None
    names = [n for n in z.namelist() if n.endswith("qualitative.htm")]
    if not names:
        return None
    return parse_qualitative(z.read(names[0]).decode("utf-8", errors="replace"), cum_sales)


# ---------------------------------------------------------------- 取り込み

def update(cfg: dict, data_dir: Path = DATA_DIR, session: RateLimitedSession | None = None,
           limit: int = 300) -> int:
    """まだ読んでいない決算短信の受注の表を読む(表が無い短信も status=none で記録し、取り直さない)。"""
    disc = disclosures.load(data_dir)
    have = set(load(data_dir)["disclosure_id"])
    arch = {a["disclosure_id"]: a for a in earnings.load_archive(data_dir).to_dict("records")}
    todo = disc[(disc["kind"] == "earnings_report") & (disc["is_correction"] != "True") & (disc["xbrl_url"] != "")
                & ~disc["disclosure_id"].isin(have)].sort_values("disclosed_at").head(limit)
    if todo.empty:
        return 0
    session = session or RateLimitedSession.from_config(cfg)
    rows = []
    for r in todo.itertuples():
        a = arch.get(r.disclosure_id) or {}
        try:
            resp = session.get(r.xbrl_url)
        except Exception as e:                         # 次回再試行
            log.warning("受注: XBRL取得失敗 %s: %s", r.code, e)
            continue
        if resp is None:
            continue
        sales = pd.to_numeric(a.get("cum_sales"), errors="coerce")
        try:
            x = parse_zip(resp.content, None if pd.isna(sales) else float(sales))
        except Exception as e:                         # 表の形が想定外でも止めない
            log.warning("受注: 表の読み取り失敗 %s: %s", r.code, e)
            x = None
        rows.append({"disclosure_id": r.disclosure_id, "code": r.code, "disclosed_at": r.disclosed_at,
                     "n_q": a.get("n_q", ""), "period_end": a.get("period_end", ""),
                     "status": "ok" if x else "none",
                     "data": json.dumps(x, ensure_ascii=False) if x else ""})
    if rows:
        df = pd.concat([load(data_dir), pd.DataFrame(rows).reindex(columns=COLUMNS)])
        df.sort_values(["disclosed_at", "disclosure_id"], ascending=False).to_csv(path(data_dir), index=False,
                                                                                encoding="utf-8")
    log.info("orders: %d (ok %d)", len(rows), sum(r["status"] == "ok" for r in rows))
    return len(rows)
