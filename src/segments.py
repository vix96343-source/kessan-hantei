"""決算短信のセグメント情報(XBRL の …sg…-ixbrl.htm)から、セグメント別の売上高・利益を data/segments.csv にためる。

表を読むのではなく XBRL の数値(セグメントごとのメンバー × 当期/前年同期)をそのまま使う。
セグメント名は短信の XBRL に入っている日本語ラベル(…-lab.xml)。金額は円・期首からの累計。
"""
import io
import json
import logging
import math
import re
import unicodedata
import zipfile
from pathlib import Path

import pandas as pd

from . import disclosures, earnings
from .config import DATA_DIR
from .datasources import tdnet
from .datasources.http import RateLimitedSession

log = logging.getLogger(__name__)

COLUMNS = ["disclosure_id", "code", "disclosed_at", "n_q", "period_end", "status", "data"]

# 売上は「外部顧客への売上高」(短信の本文の数値。セグメント間の取引を除く)。無ければ「計」(セグメント間を含む)。
# 利益は上から順に、あるものを使う
EXTERNAL = ["RevenuesFromExternalCustomers", "SalesToExternalCustomersIFRS", "RevenueFromExternalCustomersIFRS"]
SALES = ["NetSales", "OperatingRevenue1", "NetSalesIFRS", "RevenueIFRS", "RevenueFromContractsWithCustomers",
         "Revenue", "OperatingRevenueIFRS"]
PROFIT = ["OperatingIncome", "OperatingProfitLossIFRS", "SegmentProfitLossIFRS", "BusinessProfitLossIFRS",
          "OrdinaryIncome", "ProfitLossBeforeTaxIFRS"]
# 中間期は「InterimDuration」(Current が付かない)
# セグメントの名前の末尾は普通 Member だが、会社独自で Segment などのこともある
_CTX = re.compile(r"^(Current|Prior1)?(YTD|Interim|Year)Duration_(?:NonConsolidatedMember_)?(.+)$")
STANDARD = {"OperatingSegmentsNotIncludedInReportableSegmentsAndOtherRevenueGeneratingBusinessActivitiesMember": "その他",
            "OtherReportableSegmentsMember": "その他"}
_CTX_PLAIN = re.compile(r"^(Current|Prior1)?(YTD|Interim|Year)Duration(?:_NonConsolidatedMember)?$")
TOTAL = "EntityTotalMember"
SKIP = {"ReportableSegmentsMember", "TotalOfReportableSegmentsAndOthersMember", "ReconcilingItemsMember",
        "NonConsolidatedMember"}
# 会社独自の要素名(例: OperatingIncomeFromExternalCustomer, NetSalesDisclosureOf…)も名前で拾う
_EXT_NAME = re.compile(r"ExternalCustomer", re.I)
_SALES_NAME = re.compile(r"^(NetSales|Revenue|OperatingRevenue|Sales)", re.I)


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


def _ext(f: dict) -> str | None:
    """外部顧客への売上高の要素名"""
    return _pick(f, EXTERNAL) or next((n for n in f if _EXT_NAME.search(n) and not re.search(r"Inter|Elimin", n)), None)


def _sales(f: dict) -> str | None:
    """売上(計)の要素名"""
    return _pick(f, SALES) or next((n for n in f if _SALES_NAME.search(n) and not re.search(r"Inter|Cost", n)), None)


def parse_zip(zip_bytes: bytes) -> dict | None:
    """{segments: [{name, sales, sales_prior, op, op_prior}], total: {...}}(円・累計)。セグメントが2つ未満なら None。"""
    try:
        z = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile:
        return None
    files = [n for n in z.namelist() if re.search(r"/\d{7}-\w\wsg\d\d-.*ixbrl\.htm$", n)]
    if not files:
        return None
    html = z.read(files[0]).decode("utf-8", errors="replace")
    facts = tdnet._scaled_facts(html)
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

    # 外部顧客への売上高をタグ付けしている会社は、その値だけを使う(「―」のセグメントは計に逃げず空にする)
    has_ext = any(_ext(by_member[m][w]) for m in order for w in ("cur", "prior"))
    q = [n for n in z.namelist() if n.endswith("qualitative.htm")]
    qtext = ""
    if q:
        from bs4 import BeautifulSoup
        qtext = BeautifulSoup(z.read(q[0]).decode("utf-8", errors="replace"), "html.parser").get_text("\n")
    # 売上の基準: 短信の本文(セグメントの概況)の数値が「外部顧客への売上高」と「計(セグメント間を含む)」の
    # どちらと合うかで決める。本文に「売上高はセグメント間の取引を含んで」とある会社も計
    names = {m: STANDARD.get(m) or labels.get(m) for m in order if m not in SKIP and m != TOTAL}
    ext_v = {nm: by_member[m]["cur"].get(_ext(by_member[m]["cur"]) or "") for m, nm in names.items() if nm}
    tot_v = {nm: by_member[m]["cur"].get(_sales(by_member[m]["cur"]) or "") for m, nm in names.items() if nm}
    narr = narrative_sales(qtext, [nm for nm in names.values() if nm]) if qtext else {}
    inter = includes_intersegment(qtext) or choose_intersegment(narr, ext_v, tot_v)

    def values(f):
        # 本文がセグメント間を含む「計」で書かれている会社は計、それ以外は外部顧客への売上高
        s = (_sales(f) or _ext(f)) if inter else (_ext(f) or (None if has_ext else _sales(f)))
        sales = f[s] if s else None
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
        return parse_table_html(html, qtext)            # 表に XBRL のタグが付いていない短信は表そのものを読む
    # 合計は短信の表の一番右の列の値そのまま(足し算はしない。各セグメントの端数があるので足すと1ずれる):
    # 連結(四半期)財務諸表計上額(メンバー無しの文脈) → 無ければ合計(報告セグメント+その他) → 計
    total = None
    plain: dict[str, dict[str, float]] = {"cur": {}, "prior": {}}
    for (name, ctx), v in facts.items():
        m = _CTX_PLAIN.match(ctx)
        if m:
            plain["prior" if m.group(1) == "Prior1" else "cur"][name] = v
    # 連結の計上額でない(調整額を引く前の)合計は名前で分かるようにする(短信1ページ目の営業利益とは違う)
    for src, label in [(plain, "合計")] + [(by_member[m], lb) for m, lb in
                                           ((TOTAL, "合計"), ("TotalOfReportableSegmentsAndOthersMember", "合計(調整前)"),
                                            ("ReportableSegmentsMember", "計(調整前)")) if m in by_member]:
        (s, p), (sp, pp) = values(src["cur"]), values(src["prior"])
        if s is not None or p is not None:
            total = {"name": label, "sales": s, "sales_prior": sp, "op": p, "op_prior": pp}
            break
    out = {"segments": segs, "total": total, "basis": "計" if inter else "外部顧客"}
    tpl = table_template(z, segs)
    if tpl:
        tpl["basis"] = out["basis"]                    # 前の期の PDF も同じ基準で読む
        out["tpl"] = tpl                               # 前の期の短信(PDF)のセグメント表を読むための形
    return out


def parse_table_html(html: str, qtext: str = "") -> dict | None:
    """セグメント情報の表(HTML)をそのまま読む。XBRL のタグが付いていない短信用。
    売上は「外部顧客への売上高」(無ければ「計」)、利益は「セグメント利益(損失)」、合計は一番右の列。
    前年同期の表と当期の表があれば、前を前年同期・後ろを当期とする。"""
    from bs4 import BeautifulSoup
    from .orders import _grid, _num
    soup = BeautifulSoup(html, "html.parser")
    text = re.sub(r"\s", "", soup.get_text())
    m = re.search(r"単位[:：](千円|百万円|円)", text)
    unit = {"千円": 1e3, "百万円": 1e6, "円": 1.0}[m.group(1)] if m else 1e6
    tables = [t for t in soup.find_all("table")
              if "報告セグメント" in re.sub(r"\s", "", t.get_text())
              and re.search(r"セグメント(利益|損失)", re.sub(r"\s", "", t.get_text()))]

    def read(t, inter):
        grid = _grid(t)
        data = [r for r in grid if any(_num(c) is not None for c in r[1:])]
        if not data:
            return None
        num_cols = sorted({j for r in data for j, c in enumerate(r) if j > 0 and _num(c) is not None})
        head = grid[:grid.index(data[0])]
        names = [re.sub(r"\s", "", unicodedata.normalize("NFKC", "".join(dict.fromkeys(r[j] for r in head if j < len(r) and r[j]))))
                 for j in num_cols]
        ext = next((r for r in data if _EXT_ROW.search(_key(r[0]))), None)
        tot = next((r for r in data if _key(r[0]).startswith("計")), None)
        prof = next((r for r in data if re.match(r"セグメント(利益|損失)", _key(r[0]))), None)
        sales_row = (tot or ext) if inter else (ext or tot)
        if not sales_row or not prof:
            return None
        val = lambda row, j: None if j >= len(row) or _num(row[j]) is None else _num(row[j]) * unit
        segs, cols = {}, []
        for j, nm in zip(num_cols, names):
            nm = re.sub(r"報告セグメント|\(注\)\d*", "", nm)
            if not nm or re.search(r"^(計|合計)$|調整|計上額", nm):
                cols.append(None)
                continue
            cols.append(nm)
            segs[nm] = {"sales": val(sales_row, j), "op": val(prof, j)}
        last = num_cols[-1]
        # 前の期の短信(PDF)を同じ形で読むための形
        return segs, {"sales": val(sales_row, last), "op": val(prof, last)}, {"cols": cols, "n": len(num_cols), "unit": unit}

    # 売上の基準は本文(セグメントの概況)の数値と合う方(外部顧客への売上高 / 計)
    b_ext = [b for b in (read(t, False) for t in tables) if b]
    b_tot = [b for b in (read(t, True) for t in tables) if b]
    if not b_ext:
        return None
    ext_v = {nm: v["sales"] for nm, v in b_ext[-1][0].items()}
    tot_v = {nm: v["sales"] for nm, v in b_tot[-1][0].items()} if b_tot else {}
    inter = bool(qtext) and (includes_intersegment(qtext) or
                             choose_intersegment(narrative_sales(qtext, list(ext_v)), ext_v, tot_v))
    blocks = b_tot if inter and b_tot else b_ext
    cur, ctot, tpl = blocks[-1]
    prior, ptot, _ = blocks[0] if len(blocks) >= 2 else ({}, {}, None)
    segs = [{"name": nm, "sales": v["sales"], "op": v["op"],
             "sales_prior": (prior.get(nm) or {}).get("sales"), "op_prior": (prior.get(nm) or {}).get("op")}
            for nm, v in cur.items()]
    if len(segs) < 2:
        return None
    total = {"name": "合計", "sales": ctot["sales"], "op": ctot["op"],
             "sales_prior": ptot.get("sales"), "op_prior": ptot.get("op")}
    basis = "計" if inter else "外部顧客"
    return {"segments": segs, "total": total, "basis": basis, "tpl": {**tpl, "basis": basis}}


# ---------------------------------------------------------------- 前の期の短信(PDF)のセグメント表

def _key(name: str) -> str:
    return re.sub(r"\s|・", "", unicodedata.normalize("NFKC", name))


def table_template(z: zipfile.ZipFile, segs: list[dict]) -> dict | None:
    """短信本文(qualitative.htm)のセグメント情報の表の形: 数値の列の並び(各セグメント・計・調整額・連結)と単位。
    当期の表は前年同期の表の後ろにある(最後の表を使う)。"""
    from bs4 import BeautifulSoup
    from .orders import _grid, _num
    # 表は短信本文(qualitative.htm)にある会社と、セグメント情報の注記のファイル(…sg…ixbrl.htm)にある会社がある
    names = [n for n in z.namelist() if n.endswith("qualitative.htm")] + \
        [n for n in z.namelist() if re.search(r"/\d{7}-\w\wsg\d\d-.*ixbrl\.htm$", n)]
    seg_cores = [re.sub(r"(事業|業)$", "", _key(sg["name"])) for sg in segs if sg["name"] != "その他"]

    def score(t):
        """セグメント情報の数値の表らしさ: 売上(外部顧客・計・売上高)の行と利益(セグメント利益・営業利益)の行に数値があり、
        見出しにセグメントの名前が出てくる数。表でなければ -1"""
        txt = re.sub(r"\s", "", t.get_text())
        rows = [r for r in _grid(t) if r and any(_num(c) is not None for c in r[1:])]
        has_sales = any(_EXT_ROW.search(_key(r[0])) or _key(r[0]).startswith("計") or
                        re.fullmatch(r"売上(高|収益)|営業収益", _key(r[0])) for r in rows)
        has_profit = re.search(r"セグメント(利益|損失)", txt) or \
            any(re.match(r"営業(利益|損失)", _key(r[0])) for r in rows)
        if not (has_sales and has_profit):
            return -1
        hits = sum(1 for c in seg_cores if len(c) >= 2 and c in txt)
        # 名前が2つ以上見出しにあるか、「セグメント利益」の行がある表だけ(連結の損益計算書の表を拾わない)
        return hits if hits >= 2 or re.search(r"セグメント(利益|損失)", txt) else -1

    tables = []
    for name in names:
        soup = BeautifulSoup(z.read(name).decode("utf-8", errors="replace"), "html.parser")
        scored = [(score(t), k, t) for k, t in enumerate(soup.find_all("table"))]
        scored = [x for x in scored if x[0] >= 0]
        if scored:
            best = max(x[0] for x in scored)
            tables = [t for sc, k, t in scored if sc == best]     # 名前の一番多い表の、最後のもの(当期)
            break
    if not tables:
        return None
    grid = _grid(tables[-1])
    data = [r for r in grid if any(_num(c) is not None for c in r[1:])]
    if not data:
        return None
    width = max(len(r) for r in grid)
    num_cols = sorted({j for r in data for j, c in enumerate(r) if j > 0 and _num(c) is not None})
    head_rows = grid[:grid.index(data[0])]
    header = [" ".join(dict.fromkeys(r[j] for r in head_rows if j < len(r) and r[j])) for j in range(width)]
    cols = [header[j] for j in num_cols]
    # 列 → セグメント名(XBRL のラベルと空白を除いて照合)
    seg_keys = {_key(sg["name"]): sg["name"] for sg in segs}
    core = lambda k: re.sub(r"(事業|業)$", "", k)
    col_seg = []
    for h in cols:
        hk = _key(h)
        # XBRL の名前と表の見出しは「百貨店業」「百貨店 事業」のように末尾が違うことがあるので、末尾の「事業・業」を除いても照合
        hits = [(kk, v) for kk, v in seg_keys.items() if kk and (kk in hk or (len(core(kk)) >= 2 and core(kk) in hk))]
        # 完全一致を先に、無ければ長い名前から(「小売」が「小売周辺」の列に付かないように)
        hits.sort(key=lambda kv: (kv[0] != hk and core(kv[0]) != core(hk), -len(kv[0])))
        col_seg.append(hits[0][1] if hits else None)
    seen = set()
    for i, c in enumerate(col_seg):
        if c in seen:
            col_seg[i] = None
        elif c:
            seen.add(c)
    if sum(1 for c in col_seg if c) < 2:
        # 名前が XBRL と表で違う会社(例: 住宅建材事業 / 木質建材事業)は、左から順に並んでいるとみなす
        # (「計」「合計」「調整」「計上」「その他」の列の手前までの列数がセグメントの数と同じときだけ)
        lead = 0
        for h in cols:
            if re.search(r"計|調整|その他", _key(h)):
                break
            lead += 1
        named = [sg["name"] for sg in segs if sg["name"] != "その他"]
        if lead != len(named) or lead < 2:
            return None
        col_seg = named + [None] * (len(cols) - lead)
        if "その他" in [sg["name"] for sg in segs]:
            k = next((i for i, h in enumerate(cols) if "その他" in _key(h)), None)
            if k is not None:
                col_seg[k] = "その他"
    # 単位: 表の「計」の行と XBRL の売上の比
    sales_row = next((r for r in data if _EXT_ROW.search(_key(r[0]))), None) or \
        next((r for r in data if _key(r[0]).startswith("計")), None) or \
        next((r for r in data if re.fullmatch(r"売上(高|収益)|営業収益", _key(r[0]))), None)
    unit = None
    if sales_row:
        for j, sg in zip(num_cols, col_seg):
            v = _num(sales_row[j]) if j < len(sales_row) else None
            xs = next((x["sales"] for x in segs if x["name"] == sg), None) if sg else None
            if v and xs:
                unit = 10 ** round(math.log10(abs(xs / v)))
                break
    return {"cols": col_seg, "n": len(num_cols), "unit": unit or 1e6}


# 外部顧客への売上高の行(日本基準「外部顧客への売上高」「外部顧客に対する売上高」、IFRS「外部収益」「外部売上収益」)
_EXT_ROW = re.compile(r"外部(顧客|収益|売上)")
# 短信の本文に「(セグメント別の)売上高はセグメント間の取引を含んでおり」とある会社は、本文の数値が「計」(セグメント間を含む)
_INCL_INTER = re.compile(r"売上(高|収益)?は、?セグメント間の(内部)?(売上高?|取引|振替高?|収益)[^。]{0,12}を?含(んで|め|む)")


def includes_intersegment(text: str) -> bool:
    return bool(_INCL_INTER.search(re.sub(r"\s", "", text)))


_UNITS = {"百万円": 1e6, "千円": 1e3, "億円": 1e8}
_NARR_NUM = r"(△|▲|-|マイナス)?([\d,]+(?:\.\d+)?)(百万円|千円|億円)"


def narrative_sales(text: str, names: list[str]) -> dict[str, tuple[float, float]]:
    """短信の本文(セグメントの概況)にある各セグメントの売上: {名前: (値(円), 単位)}。
    セグメント名だけの短い行(見出し)から次の見出しまでの間で、最後の「売上高は X」(その結果…のまとめの文)を読む"""
    lines = [unicodedata.normalize("NFKC", re.sub(r"\s", "", l)) for l in text.split("\n")]
    lines = [l for l in lines if l]
    keys = [(n, re.sub(r"\s|・", "", unicodedata.normalize("NFKC", n))) for n in names]
    heads = []
    for i, l in enumerate(lines):
        lk = l.replace("・", "")
        for n, k in keys:
            core = re.sub(r"(事業|業)$", "", k)
            if len(l) <= len(k) + 12 and (k in lk or (len(core) >= 2 and core in lk)) and not re.search(r"\d,\d{3}", l):
                heads.append((i, n))
                break
    out = {}
    for h, (i, n) in enumerate(heads):
        end = heads[h + 1][0] if h + 1 < len(heads) else min(i + 30, len(lines))
        ms = list(re.finditer(r"売上(?:高|収益)(?:は|が)" + _NARR_NUM, "".join(lines[i + 1:end])))
        if ms and n not in out:
            m = ms[-1]
            out[n] = (float(m.group(2).replace(",", "")) * _UNITS[m.group(3)] * (-1 if m.group(1) else 1), _UNITS[m.group(3)])
    return out


def choose_intersegment(narr: dict, ext: dict, tot: dict) -> bool:
    """本文の売上が「外部顧客への売上高」と「計(セグメント間を含む)」のどちらと合うか。計の方が多く合えば True"""
    def hits(vals):
        return sum(1 for n, (v, u) in narr.items() if vals.get(n) is not None and abs(vals[n] - v) <= u * 1.01)
    return hits(tot) > hits(ext)
_PDF_NUM = re.compile(r"^[△▲\-－−]?[\d,]+(\.\d+)?$|^[-－―‐]$")


def _pdf_row(line: str, n: int):
    toks = re.sub(r"([△▲])\s+(\d)", r"\1\2", line).split()
    # マイナスを括弧で書く会社: (99,723) → △99,723
    toks = [re.sub(r"^[(（]([\d,]+)[)）]$", r"△\1", t) for t in toks]
    k = len(toks)
    while k > 0 and _PDF_NUM.match(toks[k - 1]):
        k -= 1
    vals = toks[k:]
    return ("".join(toks[:k]), vals) if len(vals) == n else None


def parse_pdf_text(text: str, tpl: dict) -> dict | None:
    """前の期の短信 PDF の文字から、セグメントごとの売上(外部顧客の行)・利益(セグメント利益の行)を読む。
    前年同期の表と当期の表があれば、前を前年同期・後ろを当期とする。
    今回の短信の表で空欄だった右側の列(調整額など)が PDF では「－」で埋まっていることがあるので、
    右に1〜2列多い形・1列少ない形も試す(セグメントの列は左から同じ並び)。"""
    n, cols = tpl["n"], tpl["cols"]
    tries = [(n, cols), (n + 1, cols + [None]), (n + 2, cols + [None, None])]
    # 前の期の表に無いセグメント(その後に新しくできた事業)は、表の見出しに名前が無い。その列を除いた形を先に試す
    lines = [l for l in text.splitlines() if l.strip()]
    heads = []
    for i, l in enumerate(lines):
        if "報告セグメント" in _key(l):
            h = []
            for l2 in lines[i:i + 15]:
                if len(re.findall(r"\d,\d{3}", l2)) >= 2:
                    break
                h.append(_key(l2))
            heads.append("".join(h))
    if heads:
        core = lambda c: re.sub(r"(事業|業)$", "", _key(c))
        missing = [c for c in cols if c and c != "その他" and not any(core(c) in h for h in heads)]
        if missing and len(missing) < sum(1 for c in cols if c):
            c2 = [c for c in cols if c not in missing]
            n2 = n - len(missing)
            tries = [(n2, c2), (n2 + 1, c2 + [None]), (n2 + 2, c2 + [None, None])] + tries
    # 右側の空欄でない列(計・調整額など)が前の期の表に無い形: 右端の None の列を1つずつ減らして試す
    k = 1
    while k < n and cols[-k] is None and sum(1 for c in cols[:-k] if c) >= 2:
        tries.append((n - k, cols[:-k]))
        k += 1
    for n2, c2 in tries:
        x = _parse_pdf_text(text, {**tpl, "n": n2, "cols": c2})
        if x:
            return x
    return None


def _parse_pdf_text(text: str, tpl: dict) -> dict | None:
    from .orders import _num
    n, unit, cols = tpl["n"], tpl["unit"], tpl["cols"]
    # 本文が「計」(セグメント間を含む)で書かれている会社: 今回の短信で決めた基準(tpl)か、PDF の本文の断り書き
    inter = tpl.get("basis") == "計" or includes_intersegment(text)
    lines = [l for l in text.splitlines() if l.strip()]
    blocks = []                                        # [(売上の行の値, 利益の行の値, 単位)]
    sales = ext = None
    cur_unit = unit
    for i, l in enumerate(lines):
        mu = re.search(r"単位\s*[:：]\s*(百万円|千円)", l)
        if mu:                                         # 表のすぐ上の「(単位：千円)」(前の期で単位を変えた会社がある)
            cur_unit = {"百万円": 1e6, "千円": 1e3}[mu.group(1)]
        r = _pdf_row(l, n)
        if not r:
            continue
        label, vals = r
        lab = _key(label)
        # 外部顧客への売上高(短信の本文の数値)。見出しが折り返して「外部顧客への」「売上高」が上の行にあり、
        # 数値の行に見出しが無い形も(上の数値の無い行を3行まで見る)
        above = ""
        for k in range(i - 1, max(i - 4, -1), -1):
            if _pdf_row(lines[k], n) or re.search(r"\d,\d{3}", lines[k]):
                break
            above = _key(lines[k]) + above
        # 本文が「計」で書かれている会社(inter)は外部顧客の行を使わず、計の行を使う
        if not inter and (_EXT_ROW.search(lab) or (_EXT_ROW.search(above) and
                          (not lab or (re.search(r"売上|収益", lab) and not re.search(r"内部|セグメント間|振替", lab))))):
            ext = vals
        # 計の行(IFRS は「合計」)。「計」が無く「売上収益」「売上高」の1行だけの表(ファーストリテイリング等)はその行
        elif (lab.startswith("計") and len(lab) <= 3) or lab == "合計" or                 (re.fullmatch(r"売上(高|収益)|営業収益", lab) and not sales):
            sales = ext or vals
            ext = None
        elif (sales or ext) and (re.match(r"セグメント(利益|損失)|営業(利益|損失)", lab) or
                        # 見出しが折り返して数値の行に「(△)」「又は損失(△)」「失(△)」「利益」が残る形、
                        # 見出しと数値の間に「(注)1」の行がある形(上の2行に「セグメント利益…」か「…セグメント」)
                        ((not re.search(r"[^\s()（）△▲]", lab) or re.match(r"(又は)?(損失|利益)", lab) or len(lab) <= 6) and i > 0
                         and any(re.search(r"セグメント(利益|損失)|セグメント$", _key(lines[k]))
                                 for k in range(max(i - 2, 0), i) if not _pdf_row(lines[k], n)))):
            blocks.append((sales or ext, vals, cur_unit))   # 計の行が無い表は外部顧客の行
            sales = ext = None
    if not blocks:
        return None

    def total_of(block):                               # 表の一番右の列(計上額、無ければ合計)
        sv, pv, u = _num(block[0][-1]), _num(block[1][-1]), block[2]
        return {"sales": None if sv is None else sv * u, "op": None if pv is None else pv * u}

    def to_segs(block):
        out = {}
        for j, sg in enumerate(cols):
            if not sg:
                continue
            sv, pv, u = _num(block[0][j]), _num(block[1][j]), block[2]
            out[sg] = {"sales": None if sv is None else sv * u, "op": None if pv is None else pv * u}
        return out

    cur = to_segs(blocks[-1])
    prior = to_segs(blocks[0]) if len(blocks) >= 2 else {}
    segs = [{"name": sg, "sales": v["sales"], "op": v["op"],
             "sales_prior": (prior.get(sg) or {}).get("sales"), "op_prior": (prior.get(sg) or {}).get("op")}
            for sg, v in cur.items()]
    if len(segs) < 2:
        return None
    tc, tp = total_of(blocks[-1]), (total_of(blocks[0]) if len(blocks) >= 2 else {})
    total = {"name": "合計", "sales": tc["sales"], "op": tc["op"],
             "sales_prior": tp.get("sales"), "op_prior": tp.get("op")}
    return {"segments": segs, "total": total, "src": "irbank_pdf", "basis": "計" if inter else "外部顧客"}


def plausible(code: str, ym: str, n_q: int, x: dict, hist: dict) -> bool:
    """PDF から読んだセグメントの売上の合計が、その期の連結売上(過去の四半期業績の累計)と見合うか。
    セグメントの「計」はセグメント間の売上を含むので少し大きくなる。連結売上が分からなければ判定しない。"""
    from .orders import _ym_shift
    qs = [hist.get(code, {}).get(_ym_shift(ym, -3 * k)) for k in range(n_q)]
    if any(q is None or q.get("sales") is None for q in qs):
        return True
    cons = sum(q["sales"] for q in qs)
    seg = sum(sg.get("sales") or 0 for sg in x["segments"])
    return bool(cons) and 0.85 <= seg / cons <= 1.25 and consistent(x)


def consistent(x: dict) -> bool:
    """セグメントの売上(外部顧客)の和が表の合計を超えていないか。超えるのは列が合っていない(セグメントの組み替え前の表を
    今の形で読んだ)とき。ただし表に小計の列がある会社(例: 海外 = アセアン + 中国)は、その列を除けば合計に合う"""
    t = (x.get("total") or {}).get("sales")
    vals = [sg["sales"] for sg in x["segments"] if sg.get("sales") is not None]
    if not t or not vals:
        return True
    if x.get("basis") == "計":
        # 計(セグメント間を含む)は和が合計を超えるのが普通。1つの列だけで合計を超える(列ずれ)ときだけ合っていないとみなす
        return all(v <= t * 1.01 for v in vals)
    total = sum(vals)
    if total <= t * 1.01:
        return True
    for i, v in enumerate(vals):                      # 小計の列 = すぐ前の2つ以上の列の和、かつ除けば合計に合う
        if abs(total - v - t) <= abs(t) * 0.01 and                 any(abs(sum(vals[j:i]) - v) <= max(abs(v) * 0.01, 1) for j in range(0, i - 1)):
            return True
    return False


def backfill_history(cfg: dict, depth: int = 6, data_dir: Path = DATA_DIR,
                     session: RateLimitedSession | None = None) -> dict:
    """セグメントの3か月の値と YoY に要る過去の四半期(既定: 直近6四半期 = 前年の同じ四半期とその前まで)を、
    IRBANK が保管している決算短信 PDF のセグメント情報の表から読む。期末は短信の表題から決める。手元でのみ実行。"""
    from pypdf import PdfReader
    from . import history
    from .datasources import irbank
    from .orders import IRBANK_PDF, _irbank_earnings, _ym_shift

    session = session or irbank.session_from_config(cfg)
    df = load(data_dir)
    hist = {c: {q["end"]: q for q in v} for c, v in history.by_code(history.load(data_dir)).items()}
    arch = {a["disclosure_id"]: a for a in earnings.load_archive(data_dir).to_dict("records")}
    have = {(r["code"], str(r["period_end"])[:7]) for r in df.to_dict("records") if r["period_end"] and r["status"] == "ok"}
    latest = {}
    for r in df[df["status"] == "ok"].sort_values("disclosed_at").to_dict("records"):
        a = arch.get(r["disclosure_id"])
        if a and not r["period_end"]:
            r["n_q"], r["period_end"] = a["n_q"], a["period_end"]
        d = json.loads(r["data"])
        if d.get("tpl") and r["period_end"]:
            latest[r["code"]] = {**r, "data": d}
    rows, stats = [], {"codes": 0, "pdfs": 0, "ok": 0}
    for code, cur in latest.items():
        end = str(cur["period_end"])[:7]
        floor = _ym_shift(end, -3 * depth)
        try:
            items = [(i, q, ym) for i, q, ym in _irbank_earnings(session, code)
                     if ym and floor <= ym < end and (code, ym) not in have]
        except Exception as e:
            log.warning("セグメントの推移: IRBANK 一覧の取得失敗 %s: %s", code, e)
            continue
        stats["codes"] += 1
        seen = set()
        for did, q, ym in items:
            if ym in seen:
                continue
            seen.add(ym)
            x = None
            try:
                resp = session.get(IRBANK_PDF.format(doc=did[4:]))
                stats["pdfs"] += 1
                if resp is not None:
                    text = "\n".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(resp.content)).pages)
                    x = parse_pdf_text(text, cur["data"]["tpl"])
                    if x and not plausible(code, ym, q, x, hist):
                        x = None
            except Exception as e:
                log.warning("セグメントの推移: PDF の読み取り失敗 %s %s: %s", code, did, e)
            if x:
                stats["ok"] += 1
            rows.append({"disclosure_id": did, "code": code, "disclosed_at": f"{did[4:8]}-{did[8:10]}-{did[10:12]}T00:00",
                         "n_q": str(q), "period_end": ym, "status": "ok" if x else "none",
                         "data": json.dumps(x, ensure_ascii=False) if x else ""})
    if rows:
        new = pd.DataFrame(rows).reindex(columns=COLUMNS)
        out = pd.concat([df[~df["disclosure_id"].isin(new["disclosure_id"])], new])
        out = out.sort_values(["disclosed_at", "disclosure_id"], ascending=False)
        # 同じ会社・同じ期で、読めた行があれば読めなかった行は捨てる
        okk = {(r["code"], str(r["period_end"])[:7]) for r in out.to_dict("records") if r["status"] == "ok"}
        out = out[~((out["status"] != "ok") & out.apply(lambda r: (r["code"], str(r["period_end"])[:7]) in okk, axis=1))]
        out.to_csv(path(data_dir), index=False, encoding="utf-8")
    log.info("segments history: %s", stats)
    return stats


def backfill(cfg: dict, data_dir: Path = DATA_DIR, session: RateLimitedSession | None = None) -> dict:
    """セグメント別の3か月の値を出すのに要る前の四半期の記録が無い会社は、IRBANK が保管している前の期の
    決算短信 PDF のセグメント情報の表を、今回と同じ列の並びで読んで補う(手元でのみ。Actions からは IRBANK が使えない)。"""
    from pypdf import PdfReader
    from .datasources import irbank
    from .orders import IRBANK_PDF, _irbank_earnings_ids, _ym_shift

    from . import history
    session = session or irbank.session_from_config(cfg)
    df = load(data_dir)
    hist = {c: {q["end"]: q for q in v} for c, v in history.by_code(history.load(data_dir)).items()}
    arch = {a["disclosure_id"]: a for a in earnings.load_archive(data_dir).to_dict("records")}
    have = {(r["code"], str(r["period_end"])[:7]) for r in df.to_dict("records") if r["period_end"]}
    latest = {}
    for r in df[df["status"] == "ok"].sort_values("disclosed_at").to_dict("records"):
        a = arch.get(r["disclosure_id"])
        if a and not r["period_end"]:
            r["n_q"], r["period_end"] = a["n_q"], a["period_end"]
        d = json.loads(r["data"])
        if d.get("tpl") and r["period_end"] and r["n_q"]:
            latest[r["code"]] = {**r, "data": d}
    rows, stats = [], {"codes": 0, "pdfs": 0, "ok": 0}
    for code, cur in latest.items():
        n_q, end = int(float(cur["n_q"])), str(cur["period_end"])[:7]
        back = 1 if n_q == 2 else 2
        need = [(k, _ym_shift(end, -3 * k), (n_q - k - 1) % 4 + 1) for k in range(1, back + 1)]
        need = [x for x in need if (code, x[1]) not in have]
        if not need:
            continue
        stats["codes"] += 1
        try:
            floor = f"1401{int(cur['disclosure_id'][4:8]) - 1}{cur['disclosure_id'][8:]}"
            ids = [x for x in _irbank_earnings_ids(session, code) if floor < x[0] < cur["disclosure_id"]]
        except Exception as e:
            log.warning("セグメントの補完: IRBANK 一覧の取得失敗 %s: %s", code, e)
            continue
        for k, ym, nq in need:
            did = next((i for i, q in ids if q == nq), None)
            if did is None:
                continue
            x = None
            try:
                resp = session.get(IRBANK_PDF.format(doc=did[4:]))
                stats["pdfs"] += 1
                if resp is not None:
                    text = "\n".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(resp.content)).pages)
                    x = parse_pdf_text(text, cur["data"]["tpl"])
                    if x and not plausible(code, ym, nq, x, hist):
                        log.info("セグメントの補完: 連結売上と合わないので捨てる %s %s", code, ym)
                        x = None
            except Exception as e:
                log.warning("セグメントの補完: PDF の読み取り失敗 %s %s: %s", code, did, e)
            if x:
                stats["ok"] += 1
            rows.append({"disclosure_id": did, "code": code, "disclosed_at": f"{did[4:8]}-{did[8:10]}-{did[10:12]}T00:00",
                         "n_q": str(nq), "period_end": ym, "status": "ok" if x else "none",
                         "data": json.dumps(x, ensure_ascii=False) if x else ""})
    if rows:
        new = pd.DataFrame(rows).reindex(columns=COLUMNS)
        out = pd.concat([df[~df["disclosure_id"].isin(new["disclosure_id"])], new])
        out.sort_values(["disclosed_at", "disclosure_id"], ascending=False).to_csv(path(data_dir), index=False,
                                                                                 encoding="utf-8")
    log.info("segments backfill: %s", stats)
    return stats


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
