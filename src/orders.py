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
from .datasources import tdnet
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
    t = re.sub(r"(百万円|千円|億円)$", "", t)          # 「48,926百万円」(松井建設・東鉄工業など)
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
        row, ci, col, stack = [], 0, 0, []
        while ci < len(cells) or any(k >= col for k in spans):
            if col in spans:
                spans[col][0] -= 1
                row.append(spans[col][1])
                stack.append(None)
                if spans[col][0] <= 0:
                    del spans[col]
                col += 1
                continue
            if ci >= len(cells):
                row.append("")
                stack.append(None)
                col += 1
                continue
            c = cells[ci]
            ci += 1
            text = _clean(c.get_text(" ", strip=True))
            cs = int(re.sub(r"\D", "", c.get("colspan", "1")) or 1)
            rs = int(re.sub(r"\D", "", c.get("rowspan", "1")) or 1)
            ps = [_clean(p.get_text(" ", strip=True)) for p in c.find_all("p")]
            ps = [t for t in ps if t]
            while ps and re.fullmatch(r"[%％]", ps[0]):
                ps = ps[1:]                            # 列の頭の「%」(構成比の単位)
            for _ in range(cs):
                row.append(text)
                stack.append(ps if len(ps) >= 3 else None)
                if rs > 1:
                    spans[col] = [rs - 1, text]
                col += 1
        grid.extend(_unstack(row, stack))
    width = max((len(r) for r in grid), default=0)
    return [r + [""] * (width - len(r)) for r in grid]


def _unstack(row: list[str], stack: list[list[str] | None]) -> list[list[str]]:
    """1つのセルに何行分もの値が段落(<p>)で積まれている行(タクミナ: 品目7つが1セル、金額7つが1セル)を行に分ける。
    同じ段数(3以上)の数値のセルが2つ以上あり、行見出しのセルも同じ段数のときだけ。段の無いセルは各行にそのまま"""
    sizes = {}
    for t, ps in zip(row, stack):
        if ps:
            sizes.setdefault(len(ps), []).append(ps)
    for n, cells in sorted(sizes.items(), key=lambda kv: -len(kv[1])):
        nums = [ps for ps in cells if all(_num(v) is not None or re.fullmatch(r"[－―\-—–‐]", v) for v in ps)]
        labels = [ps for ps in cells if all(_num(v) is None for v in ps)]
        if len(nums) >= 2 and labels:
            return [[(ps[i] if ps and len(ps) == n else t) for t, ps in zip(row, stack)] for i in range(n)]
        break
    return [row]


def _metric(label: str) -> str | None:
    l = re.sub(r"\s", "", label)
    if re.search(r"件|損失|前期繰越|期首", l):         # 期首の繰越残高は期末の受注残ではない
        return None
    if re.search(r"受注残|手持受注|次期繰越|繰越工事高|^手持(工事)?高|^繰越高", l):   # 手持高: 東亜建設、繰越高: 守谷商会
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


def parse_table(table, ctx: str | None = None, head: list[list[str]] | None = None) -> tuple[list[dict], float | None, dict | None]:
    """1つの表から事実のリスト [{seg, metric, period, ...}]・単位(円/単位)・表の形(前の期の PDF を読むのに使う)を返す。
    head: 直前の見出しだけの表の行(見出しと数値が別の表に分かれている短信、髙松)"""
    grid = (head or []) + _grid(table)
    if not grid:
        return [], None, None
    is_data = [any(_num(c) is not None for c in row) for row in grid]
    if not any(is_data):
        return [], None, None
    first = is_data.index(True)
    width = len(grid[0])
    numeric_cols = sorted({j for i, row in enumerate(grid) if is_data[i] for j, c in enumerate(row)
                           if _num(c) is not None})
    parents = _parents(grid, is_data, numeric_cols)
    data = [row for i, row in enumerate(grid) if is_data[i]]
    # 業績予想・受注予想の表(見出しが「予想数値」等で実績の列が無い)は読まない(東亜建設の通期個別受注予想を受注高と取り違えた)
    head = re.sub(r"\s", "", " ".join(c for row in grid[:first] for c in row))
    if re.search(r"予想|計画|見通し", head) and not re.search(r"実績|当|中間|四半期|累計", head):
        return [], None, None
    # 「前期実績 | 今期予想」の列の表(福田組の受注高予想(通期))は、期中の実績の列(当・累計・四半期・中間)が無ければ読まない。
    # 実績の列があれば予想の列だけ空にする
    col_head = {j: re.sub(r"\s", "", " ".join(row[j] for row in grid[:first] if j < len(row))) for j in numeric_cols}
    fc = [j for j in numeric_cols if re.search(r"予想|計画|見通し", col_head[j])]
    if fc:
        if not any(re.search(r"当|累計|四半期|中間", col_head[j]) for j in numeric_cols if j not in fc):
            return [], None, None
        data = [[("" if j in fc else c) for j, c in enumerate(row)] for row in data]
    keep = [k for k, row in enumerate(data)            # 予想・計画の行も読まない
            if not re.search(r"予想|計画", re.sub(r"\s", "", " ".join(row[j] for j in range(width) if j not in numeric_cols)))]
    if len(keep) < len(data):                          # 「今期予想 | 前期実績」の表: 残る行は前の期の値なので表ごと読まない
        return [], None, None
    if not data:
        return [], None, None
    if ctx and any(re.search(r"資産|負債|自己資本|有利子|キャッシュ|利益|損失|売上|収益", re.sub(r"\s", "", row[0]))
                   for row in data if row and row[0]):
        return [], None, None
    facts, header_text = _grid_facts(grid[:first], data, numeric_cols, parents, ctx)
    if ctx:
        # 表の外の見出しで読む表は、同じ行・同じ期の値が2つ出たら読まない(年で分けた列・「民間/官公庁」が
        # 事業ごとに繰り返す建設会社の表で、前年を今期と取り違える)
        keys = [(f["seg"], f["parent"], f["metric"], f["period"], f["kind"]) for f in facts]
        if len(keys) != len(set(keys)):
            return [], None, None
    # 行が「連結」「個別(単体)」の表は会社全体の受注を2通りに書いたもの: 連結を合計とし、個別は捨てる(足さない)
    names = {re.sub(r"\s", "", f["seg"]) for f in facts}
    if "連結" in names and names & {"個別", "単体"}:
        facts = [{**f, "seg": "合計"} for f in facts if re.sub(r"\s", "", f["seg"]) == "連結"]
    unit = _unit(table, header_text)
    label_cols = [j for j in range(width) if j not in numeric_cols]
    tpl = {"header": grid[:first], "width": width, "numeric_cols": numeric_cols, "unit": unit,
           "rows": [[row[j] for j in label_cols] for row in data], "parents": parents, "ctx": ctx}
    return facts, unit, tpl


_HEAD_WORD = re.compile(r"受注|前年|前期|セグメント|品目|区分|名称|単位|種類|期別|金額|構成|市場|部門")


def _parents(grid: list[list[str]], is_data: list[bool], numeric_cols: list[int]) -> list[str | None]:
    """データ行ごとの親(見出しだけの行に書かれた事業名)。「計」の行まで続き、そこで終わる。
    例: 半導体・ＦＰＤ関連装置事業(見出しだけ) → 半導体関連装置 / 分析装置 / ＦＰＤ関連装置 → 計"""
    out, parent = [], None
    for i, row in enumerate(grid):
        labels = [c for j, c in enumerate(row) if j not in numeric_cols and c]
        if not is_data[i]:
            vals = {row[j] for j in numeric_cols if j < len(row)}
            text = labels[-1] if labels else ""
            if text and vals <= {"", text} and not _HEAD_WORD.search(re.sub(r"\s", "", text)):
                parent = text
            continue
        out.append(parent)
        name = re.sub(r"\s", "", labels[-1]) if labels else ""
        if parent and (name in ("計", "小計") or (name.endswith("計") and not name.endswith("合計"))):
            parent = None
    return out


def _grid_facts(header: list[list[str]], data: list[list[str]], numeric_cols: list[int],
                parents: list[str | None] | None = None, ctx: str | None = None) -> tuple[list[dict], str]:
    """見出し行とデータ行から事実のリストを作る。返り値の2つ目は見出しの文字(単位探し用)。"""
    width = len(header[0]) if header else len(data[0])
    numeric_cols = set(numeric_cols)
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
    col_fy = {j: int(m.group(1)) for j in numeric_cols
              for m in [re.search(r"(\d{4})年\d{1,2}月期", re.sub(r"\s", "", labels[j]))] if m}
    fy_cols = len(set(col_fy.values())) >= 2
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
        if fy_cols and kind == "value" and j in col_fy:
            period = "cur" if col_fy[j] == max(col_fy.values()) else "prior"
        if "前々" in re.sub(r"\s", "", labels[j]):
            kind = None                                # 前々期の列・前々期比は読まない(大東建託: 「前々中間 | 前中間 | 前々期増減率 | 当中間 | 前期増減率」)
        cols[j] = {"kind": kind, "metric": metric, "period": period,
                   "growth_label": "増減率" in re.sub(r"\s", "", labels[j])}

    # 行見出しが決算期の表(「2027年３月期中間会計期間 | 69,822 | 百万円 | 8.3 | ％」「2026年３月期… 」、松井建設):
    # 新しい年の行が今回・古い年の行が前年、数値のすぐ右が「％」の列は前年比
    def fy_of(row):
        m = re.match(r"(\d{4})年\s*\d{1,2}\s*月期", re.sub(r"\s", "", next((row[j] for j in label_cols if row[j]), "")))
        return int(m.group(1)) if m else None
    years = [fy_of(r) for r in data]
    diff_row = lambda r: re.search(r"増減|比較|前年比|前期比", re.sub(r"\s", "", " ".join(r[j] for j in label_cols)))
    year_rows = sum(1 for y in years if y) >= 2 and len({y for y in years if y}) >= 2 and \
        all(y or diff_row(r) for y, r in zip(years, data))
    # 列の見出しが無い表(見出しは前の表、大東建託の地域別)で値の列が2つ以上あると、どれが当期か分からない(前々期の値を
    # 当期と取り違えた)ので読まない
    if len(numeric_cols) >= 2 and not any(re.sub(r"\s", "", labels[j]) for j in numeric_cols):
        return [], all_header
    real_tail = bool(numeric_cols) and any(
        re.sub(r"[\s－―\-—–‐()（）%％]|百万円|千円|億円", "", row[j])
        for row in data for j in label_cols if j > min(numeric_cols) and j < len(row))
    # 見出しの一番上の段がセグメント名の表(「| 通信インフラ | 社会インフラ | システムソリューション」の下に「金額 | 増減率」、
    # 行が「受注高」「売上高」、エクシオ): 列の段の名前をセグメントにする(行見出しの「受注高」だけでは全体の値と取り違える)
    seg_of_col = {}
    if header:
        top = {j: re.sub(r"\s", "", header[0][j]) if j < len(header[0]) else "" for j in numeric_cols}
        vcols = [j for j, c in cols.items() if c["kind"] == "value"]
        names_top = {top[j] for j in vcols}
        if len(vcols) >= 2 and len(names_top) >= 2 and all(
                top[j] and not re.search(r"前|当|期|年|月|増減|比|計画|予想|実績|金額|累計|受注|売上|残", top[j]) for j in vcols):
            seg_of_col = {j: ("合計" if _TOTAL.match(top[j]) or top[j] in ("計", "連結") else header[0][j].strip())
                          for j in numeric_cols if top[j]}
    facts = []
    prev_metric = None
    for ri, row in enumerate(data):
        if year_rows:
            if not years[ri]:
                continue                               # 増減額・増減率の行
            for j, c in cols.items():
                v = _num(row[j])
                metric = c["metric"] or ctx
                if v is None or metric is None:
                    continue
                pct = re.sub(r"\s", "", row[j]).endswith(("%", "％")) or \
                    (j + 1 < width and re.sub(r"\s", "", row[j + 1]) in ("%", "％"))
                if pct and years[ri] != max(y for y in years if y):
                    continue                           # 前年の行の増減率は今回の YoY ではない
                facts.append({"seg": "合計", "metric": metric, "period": "cur" if years[ri] == max(y for y in years if y) else "prior",
                              "kind": "pct" if pct else "value", "value": v, "signed": _signed(row[j]),
                              "growth_label": False, "group": False, "parent": None})
            continue
        parent = parents[ri] if parents and ri < len(parents) else None
        names = []
        # 数値の右の見出しの列(「セグメントとの関連」、国際計測器)は行の名前にしない
        # (「－」だけの列は数値の列の空き。それしか無い表は今までどおり、アンリツ)
        lead = [j for j in label_cols if j < min(numeric_cols)] if real_tail else label_cols
        for j in lead:
            # 「( 33.2 %)」のように括弧・%が別のセルの表(東亜建設)は、その括弧や%を行の名前にしない
            if row[j] and (not names or names[-1] != row[j]) and \
                    not re.fullmatch(r"[\s()（）%％]+|百万円|千円|億円", row[j]):
                names.append(row[j])
        if not names or re.match(r"^[（(]", names[-1]):
            continue                                   # 「(非住宅物件等含む)」のような内数の行
        row_metric = next((m for m in (_metric(n) for n in names) if m), None)
        # 縦の見出し(「受注高」)の結合が「計」の行に掛かっていない表(髙松): 前の行の項目の「計」= その項目の全体
        if row_metric is None and len(names) == 1 and re.sub(r"\s", "", names[0]) in ("計", "合計") and prev_metric \
                and not row[lead[0]].strip():
            row_metric, names = prev_metric, ["合計"]
            prev_metric = None                         # 区切りの「合計」で項目が終わる
        else:
            # 見出しが区切りの途中の行(「受注高 | | 計」)にだけある表(佐藤渡辺)も、区切りの「合計」まで引き継ぐ
            prev_metric = row_metric or prev_metric
        # 行の見出しに期が書いてある表(「前連結会計年度 | 国内EPC事業 | …」「当連結会計年度 | …」)は行の期を使う
        row_period = None
        for nm in names[:-1]:
            k = re.sub(r"\s", "", nm)
            if re.match(r"前(連結|事業|第|中間|期|年)", k):
                row_period = "prior"
            elif re.match(r"当(連結|事業|第|中間|期|年)", k):
                row_period = "cur"
        seg = re.sub(r"\s*[(（]\s*うち[^)）]*[)）]\s*$", "", names[-1]) or names[-1]   # 「合 計 （ うち海外 ）」は合計(タクミナ)
        if _metric(seg) and len(names) == 1:
            seg = "合計"                               # 「受注高 | 50,163 | 59,267 …」のように行見出しが項目名
        group, child_of = False, None
        if parent:
            key = re.sub(r"\s", "", seg)
            if key in ("計", "小計") or (key.endswith("計") and not key.endswith("合計")):
                seg, group = parent, True              # 事業の「計」= その事業の受注
            else:
                child_of = parent
        for j, c in cols.items():
            if c["kind"] is None:
                continue
            metric = c["metric"] or row_metric or ctx  # ctx: 表の外の見出し(「①受注高」の表)
            v = _num(row[j])
            if metric is None or v is None:
                continue
            period = row_period if row_period and c["kind"] == "value" else c["period"]
            if row_period == "prior" and c["kind"] == "pct":
                continue                               # 前年の行の増減率は今回の YoY ではない
            col_seg = seg_of_col.get(j) if seg == "合計" and seg_of_col else None
            facts.append({"seg": col_seg or seg, "metric": metric, "period": period, "kind": c["kind"],
                          "value": v, "signed": _signed(row[j]), "growth_label": c["growth_label"],
                          "group": group, "parent": child_of})
    return facts, all_header


_UNIT_TAIL = re.compile(r"\s*[（(](千円|百万円|億円)[)）]\s*$")


def _seg_key(name: str) -> str:
    # 「合計（千円）」「建築事業（千円）」のように単位が付いた行の名前(1798)は単位を外す
    return re.sub(r"\s", "", _UNIT_TAIL.sub("", name))


def _assemble(facts: list[dict], unit: float) -> dict | None:
    """事実のリスト → {segments: [...], total: {...}}(円・増減率%)"""
    segs: dict[str, dict] = {}
    order = []
    for f in facts:
        k = _seg_key(f["seg"])
        if re.search(r"利益|EBITDA|マージン", k):        # 受注の表の中の業績の行(日本製鋼所: 営業利益・EBITDA)
            continue
        if k not in segs:
            segs[k] = {"name": _UNIT_TAIL.sub("", f["seg"])}
            order.append(k)
        s = segs[k]
        if f.get("group"):
            s["group"] = True
        if f.get("parent"):
            s["parent"] = f["parent"]
        if f["kind"] == "value":
            key = f"{f['metric']}_{f['period']}"
            # 合計が2つの表に出るとき(事業ごとの内訳の表の「合計」と会社全体の表の「合計」、日本製鋼所)は大きいほう
            if _TOTAL.match(k) and key in s and f.get("tbl") is not None and s.get(key + "_tbl") not in (None, f["tbl"])                     and abs(f["value"] * unit) > abs(s[key]):
                s[key], s[key + "_tbl"] = f["value"] * unit, f["tbl"]   # 別の表の合計のほうが大きい
            if key not in s:
                s[key], s[key + "_tbl"] = f["value"] * unit, f.get("tbl")
        else:
            s.setdefault(f"{f['metric']}_raw", f["value"])
            s[f"{f['metric']}_signed"] = s.get(f"{f['metric']}_signed", False) or f["signed"]
            s[f"{f['metric']}_growth_label"] = f["growth_label"]

    totals = [k for k in order if _TOTAL.match(k)]
    subtotals = [k for k in order if k not in totals and _SUBTOTAL.search(k) and not segs[k].get("group")]
    groups = [k for k in order if segs[k].get("group")]
    leaves = [k for k in order if k not in totals and k not in subtotals and k not in groups]
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

    for m in METRICS:                                   # 事業(group)の YoY も同じ書き方で
        style = _pct_style([segs[k] for k in leaves], total, m)
        for k in groups:
            segs[k][f"{m}_yoy"] = _yoy(segs[k], m, style)

    def out(s):
        return {"name": s["name"], **({"group": True} if s.get("group") else {}),
                **({"parent": s["parent"]} if s.get("parent") else {}),
                **{f"{m}{suf}": s.get(f"{m}_{p}") for m in METRICS
                                       for suf, p in (("", "cur"), ("_prior", "prior"), ("_fy", "fy"))},
                **{f"{m}_yoy": s.get(f"{m}_yoy") for m in METRICS}}

    t = out(total)
    if t["orders"] is None and t["backlog"] is None:
        return None
    # 表示の順: 事業の行をその品目の前に
    shown = []
    for k in order:
        if k in totals or k in subtotals or k in shown:
            continue
        par = segs[k].get("parent")
        pk = _seg_key(par) if par else None
        if pk and pk in segs and pk not in shown:
            shown.append(pk)
        if k not in shown:
            shown.append(k)
    segments = [out(segs[k]) for k in shown] if len(leaves) > 1 or groups else []
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


def _heading_metric(table) -> str | None:
    """表のすぐ上(前の表との間)の見出しが受注高・受注残高なら、その項目(orders / backlog)。
    例: 「③その他 (1)セグメント別受注高・売上高・受注残高 ①受注高 (単位：百万円)」の直後の表 → orders"""
    before = ""
    for s in table.find_all_previous(string=True, limit=20):
        pt = s.find_parent("table")
        if pt is not None:
            if re.fullmatch(r"[（(]単位[:：][^)）]*[)）]", re.sub(r"\s", "", pt.get_text(""))):
                continue                               # 「（単位：百万円）」だけの小さな表は飛ばす
            break                                      # 前の表まで戻ったら終わり
        before = str(s) + before
        if len(before) > 120:
            break
    b = re.sub(r"\s", "", before)[-60:]
    hits = list(re.finditer(r"受注残高?|手持受注|受注高|受注実績|受注額|(?<!前期)(?<!期首)繰越高", b))
    if not hits:
        return None
    rest = b[hits[-1].end():]
    # 文の中の「受注残」(「…受注残の納品が進んだこと等から、増収増益となりました。（２）財政状態の概況」、3768)は見出しではない
    if "。" in rest or re.search(r"財政|資産|負債|キャッシュ", rest):
        return None
    if re.search(r"売上|販売|生産|出荷|完成", rest):  # 「受注高・売上高・受注残高」の題の後に「②売上高」が来るとき
        return None
    return _metric(hits[-1].group(0)) or ("orders" if "受注" in hits[-1].group(0) else None)


def parse_qualitative(html: str, cum_sales: float | None = None) -> dict | None:
    """qualitative.htm 全体から受注の表を探して読む。無ければ None。"""
    soup = BeautifulSoup(html, "html.parser")
    facts, unit, tpls = [], None, []
    prev_grid = None
    for t in soup.find_all("table"):
        text = t.get_text(" ", strip=True)
        g_prev, prev_grid = prev_grid, None
        if "……" in text or len(text) > 6000:
            continue
        prev_grid = _grid(t)
        if re.search(r"流動資産|資産合計|負債合計|純資産合計|償却期間|顧客関係", re.sub(r"\s", "", text)):
            continue                                   # 財政状態・貸借対照表の表(期末残高を受注残と取り違えない)、
            #                                            企業結合で計上した無形資産の「受注残」(6395)
        ctx = None
        # すぐ上の見出しが「受注予想」「業績予想」「業績見込」の表は読まない(東急建設: 表の中に予想の字が無い通期個別受注予想の表、
        # テクノ菱和: 「連結業績見込は、次のとおり」の受注高 125,000百万円)
        above = ""
        for s in t.find_all_previous(string=True, limit=12):
            s = re.sub(r"\s", "", s)
            if s:
                above = s + above
            if len(above) >= 40:
                break
        head = above[-80:]
        for _ in range(3):                             # 「(個別)」「（2026年４月１日～…）」「(単位：百万円)」を除く
            head = re.sub(r"[(（][^()（）]*[)）]$", "", head)
        g0 = _grid(t)
        # ただし表の見出し(数値の行より上)に実績・当期の列があれば読む(大成建設「業績予想の概要」の中間期の実績の列)
        g_rows = []
        for row in g0:
            if any(_num(c) is not None for c in row):
                break
            g_rows.append(row)
        g_head = re.sub(r"\s", "", " ".join(c for row in g_rows for c in row))
        if (re.search(r"(予想|計画|見通し)$", head) or re.search(r"受注予想|業績予想|受注計画|業績見込|見込みは|見込は", above[-40:])) \
                and not re.search(r"実績|当(中間|第|四半期|期)|累計", g_head):
            continue
        title = re.sub(r"\s", "", g0[0][0]) if g0 and g0[0] else ""
        if title and all(not c.strip() or "単位" in c or c == g0[0][0] for c in g0[0]):
            if re.fullmatch(r"[①-⑩0-9０-９().（）]*(売上|販売|生産|完成)\S{0,6}", title):
                continue                                   # 売上・生産の表
            if re.fullmatch(r"[①-⑩0-9０-９().（）]*(受注残高?|繰越\S{0,4})", title):
                ctx = "backlog"
            elif re.fullmatch(r"[①-⑩0-9０-９().（）]*受注(高|実績|額)?", title):
                ctx = "orders"
        if ctx is None and not _TABLE_HINT.search(re.sub(r"\s", "", text)):
            ctx = _heading_metric(t)                   # 表の中に「受注」が無く、すぐ上の見出しが「①受注高」の表
            if ctx is None:
                continue
        # 見出しの無い表(1行目から数値)の直前が同じ列数の見出しだけの表なら、それを見出しとする
        head = None
        if g0 and g_prev and any(_num(c) is not None for c in g0[0]) and \
                not any(_num(c) is not None for r in g_prev for c in r) and len(g_prev[0]) == len(g0[0]):
            head = g_prev
        f, u, tpl = parse_table(t, ctx, head)
        if f:
            facts += [{**x, "tbl": len(tpls), "u": u} for x in f]   # どの表の値か(合計が2つの表に出る会社用)
            unit = unit or u
            tpls.append(tpl)
    if not facts:
        return None
    unit = unit or _guess_unit(facts, cum_sales)
    x = _assemble(_same_unit(facts, unit), unit)
    if x:
        x["unit"] = unit
        x["tpl"] = tpls                                # 前の期の短信(PDF)を同じ形で読むための表の形
    return x


# ---------------------------------------------------------------- 前の期の短信(PDF)

_PDF_TOKEN = re.compile(r"^([△▲\-－−+＋]?[\d,，]+(\.\d+)?(百万円|千円|億円)?[%％]?([（(][^)）]*[)）])?|[－―\-—–‐]|[（(][^)）]*[)）])$")


def _plab(label: str) -> str:
    """行見出しの期間の書き方をそろえる(今回の短信は「中間会計期間」、前の期の短信は「第１四半期累計期間」)"""
    label = re.sub(r"(第[1-4１-４]四半期|中間)(連結)?(累計|会計)?期間", "期間", label)
    # 注記の印(「(注)１」「(注１）」「※」)は期によって書き方・位置が変わる(大東建託)
    return re.sub(r"[(（]注[)）]?[0-9０-９]*[)）]?[0-9０-９]*|※", "", label)


def _undup(line: str) -> str:
    """太字を重ね書きした PDF で同じ字が5回ずつ続く行(「受注高受注高…」「66666,,,,,33333」)を1回にする(大成建設の1Q)"""
    if not re.search(r",{5}|([^\d\s,])\1{4}|(\S{2,4})\2{4}", line):
        return line
    line = re.sub(r"(\S{2,8}?)\1{4}", r"\1", line)
    return re.sub(r"(.)\1{4}", r"\1", line)


def _pdf_row(line: str, label: str, n_values: int, n_min: int | None = None,
             groups: list[int] | None = None) -> list[str] | None:
    """PDF の1行が「行見出し 数値 数値 …」で、見出しが label・数値の個数が n_values なら数値の文字を返す。
    n_min: 今回の表の左の期間(当期の累計)の列の数。前の期の表は右の通期の列の数が違うことがあるので、
    左の n_min 個だけ使い残りは空にする。
    groups: 見出しの組(前期・当期・増減額・増減率)ごとの最初の列。今回の表で「金額・率」の2列だった組が
    前の期の PDF で1つしか値が無い行(鹿島の「売上高 6,496 6,400 △95 △1.5」)は、組の最初の列に入れる"""
    toks = line.split()
    k = len(toks)
    while k > 0 and _PDF_TOKEN.match(toks[k - 1]):
        k -= 1
    if _plab(re.sub(r"\s", "", "".join(toks[:k]))) != _plab(label):
        return None
    vals = [re.sub(r"(百万円|千円|億円)", "", t) for t in toks[k:] if not re.match(r"^[（(]", t)]   # 括弧書き(内数)は捨てる
    if len(vals) != n_values:
        if groups and len(groups) < n_values and len(vals) == len(groups):
            row = [""] * n_values
            for g, v in zip(groups, vals):
                row[g] = v
            vals = row
        elif not n_min or len(vals) < n_min + 1:
            return None
        else:
            vals = vals[:n_min] + [""] * (n_values - n_min)
    return ["" if re.fullmatch(r"[－―\-—–‐]", v) else v for v in vals]


def _strip_tail(line: str, tail: str) -> str:
    """行の終わりの見出し(数値の右の列の文字、「日本(国際),米国,韓国,中国」)を取る。無ければそのまま"""
    if not tail:
        return line
    toks = line.split()
    acc = ""
    for k in range(len(toks) - 1, 0, -1):
        acc = toks[k] + acc
        if acc == tail:
            return " ".join(toks[:k])
        if len(acc) >= len(tail):
            break
    return line


def parse_pdf_text(text: str, tpls: list[dict], unit: float | None = None) -> dict | None:
    """前の期の短信 PDF の文字から、今回の短信と同じ形の受注の表を読む(会社の表の形は毎期ほぼ同じ)。"""
    # 「＋ 22,708 ＋　8.6％」のように符号と数値の間に空白がある形(1721 コムシス)は詰める
    lines = [re.sub(r"(?<!\S)([+＋△▲])[ 　]+(?=\d)", r"\1", _undup(l)) for l in text.splitlines() if l.strip()]
    facts = []
    unit = None
    done = 0                                           # 前の表の最後の行(表は今回と同じ順に並ぶ)
    for tpl in tpls:
        width, ncols = tpl["width"], tpl["numeric_cols"]
        label_cols = [j for j in range(width) if j not in ncols]

        # 金額の列(見出しが受注高・受注残など)に小数(構成比や前年比の%)が来たら、表の形が違うので合わない行とする
        heads = [" ".join(dict.fromkeys(r[j] for r in tpl["header"] if j < len(r) and r[j])) for j in range(width)]
        money = [_col_kind(heads[j]) == "value" for j in ncols]

        # 見出しの一番上が「中間会計期間 ×3、通期 ×5」のように期間ごとに分かれる表: 左の期間の列の数
        top = [tpl["header"][0][j] if tpl["header"] and j < len(tpl["header"][0]) else "" for j in ncols]
        n_min = next((k for k in range(1, len(top)) if top[k] != top[0]), None)             if top and re.search(r"期間|中間|四半期|累計", top[0]) else None

        last = [tpl["header"][-1][j] if tpl["header"] and j < len(tpl["header"][-1]) else "" for j in ncols]
        groups = [k for k in range(len(last)) if k == 0 or last[k] != last[k - 1]]
        # 組の分かれ目は見出しのどの段で分けてもよい(トーカロ: 下の段は「受注高 ×4」、上の段で前期・当期に分かれる。
        # 椿本興業: 上の段の「受注高 | 受注高 | 増減比 | 受注残高 | 受注残高 | 増減比」で分ける)。
        # 数値の列が空の段(「溶射加工(単体)」のような親の行)は見ない
        hrows = [[r[j] if j < len(r) else "" for j in ncols] for r in tpl["header"]]
        hrows = [r for r in hrows if any(r)]
        cand = [groups] + [[k for k in range(len(ncols)) if k == 0 or r[k] != r[k - 1]] for r in hrows] + \
            [[k for k in range(len(ncols)) if k == 0 or any(r[k] != r[k - 1] for r in hrows)]]
        # 組に値が1つしか無い行は、組の中の当期の列に入れる(椿本興業: 今回の表は「受注高 前第２四半期 | 当第２四半期」、
        # 1Q の短信は「受注高」の1列だけ)。当期の列が無い組(金額・率)は組の最初の列
        hl = [re.sub(r"\s", "", " ".join(r[j] for r in tpl["header"] if j < len(r))) for j in ncols]

        def slots(gs):
            out = []
            for a, k in enumerate(gs):
                end = gs[a + 1] if a + 1 < len(gs) else len(ncols)
                out.append(next((c for c in range(k, end) if "当" in hl[c]), k))
            return out
        gsets = []
        for g in cand:
            g = slots(g)
            if g not in gsets:
                gsets.append(g)

        def row_vals(line, label):
            # 値の数が列の数か組の数にちょうど合う読み方を先に。合わなければ左の期間の列だけ(n_min)
            for g in gsets:
                v = _pdf_row(line, label, len(ncols), None, g)
                if v is not None:
                    return v
            return _pdf_row(line, label, len(ncols), n_min, gsets[0]) if n_min else None

        def fits(vals):
            return all(not (m and "." in v and not v.endswith(("%", "％"))) for m, v in zip(money, vals))

        def match_from(start):
            data, pos, first_at = [], start, None
            for labels in tpl["rows"]:
                unit_only = ("%", "％", "百万円", "千円", "億円")
                lead = [n for j, n in zip(label_cols, labels) if j < min(ncols)] or list(labels)
                tail = "".join(re.sub(r"\s", "", n) for j, n in zip(label_cols, labels)
                               if j > max(ncols) and n and re.sub(r"\s", "", n) not in unit_only)
                names = [n for n in lead if n and re.sub(r"\s", "", n) not in unit_only]   # 単位だけの列は見出しでない
                label = re.sub(r"\s", "", names[-1]) if names else ""
                if not label:
                    continue
                for i in range(pos, min(pos + 40, len(lines))):
                    vals = row_vals(_strip_tail(lines[i], tail), label)
                    if vals is None and i > 0 and not re.search(r"\d", lines[i - 1]):
                        # 行見出しが2行に折り返す(「半導体・ＦＰＤ製造装置」「用部品への加工 5,881 …」、トーカロ)
                        joined = _strip_tail(lines[i - 1].strip() + lines[i], tail)
                        vals = row_vals(joined, label)
                    if vals is not None and fits(vals):
                        row = [""] * width
                        for j, n in zip(label_cols, labels):
                            row[j] = n
                        for j, v in zip(ncols, vals):
                            row[j] = v
                        data.append(row)
                        first_at = i if first_at is None else first_at
                        pos = i + 1
                        break
            # 受注・受注残が表の外の見出しの表(ctx)は、合った最初の行から上へ見て最初に出てくる表の見出し
            # (受注・受注残・売上・販売・生産)がその見出しのときだけ
            # (芝浦機械: 同じ行見出しの売上の表を受注残の表と取り違えない)
            # 表のすぐ上(3行)が生産・販売の表の見出しで受注の字が無ければ別の表(国際計測器: 同じ行見出しの生産実績の表が前にある)
            if first_at is not None:
                above = [re.sub(r"\s", "", l) for l in lines[max(0, first_at - 3):first_at]]
                if any(re.search(r"生産|販売|売上", l) for l in above) and not any(re.search(r"受注|繰越", l) for l in above):
                    return [], pos
            if tpl.get("ctx") and first_at is not None:
                word = r"受注残|繰越" if tpl["ctx"] == "backlog" else r"受注(?!残)"
                hit = next((l for l in reversed(lines[max(0, first_at - 25):first_at])
                            if re.match(r"[（(]?[0-9０-９①-⑳]*[)）．.、]?(受注|繰越|売上|販売|生産|完成)", re.sub(r"\s", "", l))
                            and not re.search(r"\d,\d{3}", l) and len(l) <= 40), "")   # 見出しの形の行だけ(数値の行・本文は除く)
                # 見出しが表の上に無い PDF(三井E&S は表の後ろに「受注高・売上高・受注残高」とまとめて出る)は
                # 見出しでは決めない(期末残高の表は下の「末」の確かめで分ける)
                if hit and not re.search(word, hit):
                    return [], pos
            return data, pos

        # 「受注」が出てくる行それぞれから探し、一番多く行が合ったものを採る
        # (目次・本文・生産実績の表に同じ行見出しがあっても、受注の表の見出しの後ろが一番よく合う)
        best, best_end, cands = [], done, []
        stock = any(re.search(r"(期間|年度|期)末", re.sub(r"\s", "", c)) for row in tpl["header"] for c in row) and \
            not any(re.search(r"累計期間|会計期間(?!末)|事業年度(?!末)", re.sub(r"\s", "", c)) for row in tpl["header"] for c in row) and \
            not any(_metric(h) == "orders" for h in hl)   # 受注高の列もある表は期末残高だけの表ではない(椿本興業)
        # 表の左の見出しが縦書きで「受」「注」「高」と1字ずつの行になる形も(2026-11-11 ピーエス・コンストラクション)
        # 表の1行目(売上高など)の行からも探す: 受注の行が表の下のほうにある表(鹿島の「ハイライト情報」)
        first = next((re.sub(r"\s", "", [n for n in r if n][-1]) for r in tpl["rows"] if any(r)), None)
        for s in (i for i in range(done, len(lines))
                  if "受注" in (lines[i] + (lines[i + 1] if i + 1 < len(lines) and len(lines[i]) == 1 else "")).replace("受注損失", "")
                  or "繰越工事" in lines[i]
                  or (first and row_vals(lines[i], first) is not None)):
            # 期末の残高の表(見出しが「…期間末」「…年度末」)は、PDF でも見出しに「末」がある表だけ
            # (三井E&S: 1Q の短信に受注残の表が無く、同じ行見出しの売上高の表を受注残と取り違えた)
            if stock and not any("末" in l for l in lines[max(0, s - 15):s + 1]):
                continue
            data, end = match_from(s)
            cands.append((s, data, end))
        # 一番多く合った数の 3/4 以上合う表のうち一番前のもの(連結の表の後ろに同じ形の個別の表がある会社。大成建設)
        most = max((len(d) for _, d, _ in cands), default=0)
        for _, data, end in cands:
            if data and len(data) * 4 >= most * 3:
                best, best_end = data, end
                break
        if not best:
            continue
        done = best_end
        par = tpl.get("parents") or []
        labels_of = {tuple(r): par[i] if i < len(par) else None for i, r in enumerate(tpl["rows"])}
        best_parents = [labels_of.get(tuple(row[j] for j in label_cols)) for row in best]
        f, _ = _grid_facts(tpl["header"], best, ncols, best_parents, tpl.get("ctx"))
        names = {re.sub(r"\s", "", x["seg"]) for x in f}
        if "連結" in names and names & {"個別", "単体"}:  # 今回の表と同じ扱い(連結を合計とし、個別は足さない。福田組)
            f = [{**x, "seg": "合計"} for x in f if re.sub(r"\s", "", x["seg"]) == "連結"]
        facts += [{**x, "u": tpl["unit"]} for x in f]
        unit = unit or tpl["unit"]
    if not facts:
        return None
    return _assemble(_same_unit(facts, unit or 1e6), unit or 1e6)


def _same_unit(facts: list[dict], unit: float) -> list[dict]:
    """表ごとに単位が違う短信(百万円の概況の表と千円のセグメント別の表、208A)の値を、全体の単位にそろえる"""
    return [{**f, "value": f["value"] * f["u"] / unit} if f["kind"] == "value" and f.get("u") and f["u"] != unit else f
            for f in facts]


IRBANK_IR = "https://irbank.net/{code}/ir"
IRBANK_PDF = "https://api.irbank.net/api/market-files/v1/tdnet/{doc}.pdf"


def _ym_shift(ym: str, months: int) -> str:
    y, m = int(ym[:4]), int(ym[5:7]) + months
    y, m = y + (m - 1) // 12, (m - 1) % 12 + 1
    return f"{y:04d}-{m:02d}"


def _quarter_of_title(title: str) -> int:
    t = title.translate(str.maketrans("１２３４", "1234"))
    m = re.search(r"第([1-3])四半期", t)
    if m:
        return int(m.group(1))
    return 2 if "中間" in t else 4


def period_end_of_title(title: str) -> str | None:
    """「2027年2月期 第1四半期決算短信」→ その四半期の期末の年月 '2026-05'(通期は期末そのもの)"""
    t = title.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    m = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月期", t)
    if not m:
        return None
    return _ym_shift(f"{int(m.group(1)):04d}-{int(m.group(2)):02d}", -3 * (4 - _quarter_of_title(title)))


def _irbank_earnings_ids(session: RateLimitedSession, code: str, fresh_after: str = "") -> list[tuple[str, int]]:
    """決算短信(訂正を除く)の (開示ID, 四半期) 新しい順"""
    return [(d, q) for d, q, _ in _irbank_earnings(session, code, fresh_after)]


def _irbank_earnings(session: RateLimitedSession, code: str, fresh_after: str = "") -> list[tuple[str, int, str | None]]:
    """決算短信(訂正を除く)の (開示ID, 四半期, 期末の年月) 新しい順。
    IRBANK Connect の API(手元の .env にキーがあるとき。索引 data/cache/irbank_index.csv に保存)から。
    無ければ IRBANK の会社別適時開示一覧のページから(2026-10-21 からメンテナンスで止まっている)"""
    from .datasources import irbank_api
    rows = irbank_api.earnings_reports(code, fresh_after=fresh_after) if irbank_api.api_key() else None
    if rows is not None:
        ids = []
        for x in rows:
            c = tdnet.classify_title(x["title"])
            did = "1401" + x["doc_id"]
            if c["kind"] == "earnings_report" and not c["is_correction"] and did not in {i[0] for i in ids}:
                ids.append((did, _quarter_of_title(x["title"]), period_end_of_title(x["title"])))
        return sorted(ids, reverse=True)
    r = session.get(IRBANK_IR.format(code=code))
    if r is None:
        return []
    soup = BeautifulSoup(r.text, "html.parser")
    ids = []
    for a in soup.find_all("a", href=re.compile(rf"^/{re.escape(code)}/1401\d{{14}}$")):
        did = a["href"].rsplit("/", 1)[-1]
        c = tdnet.classify_title(re.sub(r"[（(]\d{1,2}:\d{2}[)）]$", "", a.get_text(strip=True)))
        if c["kind"] == "earnings_report" and not c["is_correction"] and did not in {i[0] for i in ids}:
            ids.append((did, _quarter_of_title(a.get_text(strip=True)), period_end_of_title(a.get_text(strip=True))))
    return sorted(ids, reverse=True)


def fix_scale(code: str, ym: str, n_q: int, x: dict, hist: dict) -> dict:
    """PDF の表の単位が今回の短信と違う(千円と百万円など)と金額が1000倍ずれる。
    その期の連結売上(過去の四半期業績の累計)と比べて 1000 倍・1/1000 のずれを直す。"""
    qs = [hist.get(code, {}).get(_ym_shift(ym, -3 * k)) for k in range(n_q)]
    v = (x.get("total") or {}).get("orders") or (x.get("total") or {}).get("backlog")
    if v is None or any(q is None or q.get("sales") is None for q in qs):
        return x
    sales = sum(q["sales"] for q in qs)
    if not sales:
        return x
    r = abs(v / sales)
    f = 1e-3 if r > 200 else 1e3 if r < 0.002 else None
    if f is None:
        return x
    for sg in x.get("segments", []) + ([x["total"]] if x.get("total") else []):
        for k in ("orders", "orders_prior", "orders_fy", "backlog", "backlog_prior", "backlog_fy"):
            if sg.get(k) is not None:
                sg[k] *= f
    return x


def fix_scale_to(x: dict, anchor_total: dict) -> dict:
    """今回の短信(XBRL・HTML の確かな値)と比べて 100 倍以上ずれていれば、1000 倍単位で直す(千円の表など)"""
    a = anchor_total.get("backlog") or anchor_total.get("orders")
    t = x.get("total") or {}
    v = t.get("backlog") or t.get("orders")
    if not a or not v:
        return x
    f = 1.0
    while abs(v) * f > abs(a) * 100:
        f *= 1e-3
    while abs(v) * f < abs(a) / 100:
        f *= 1e3
    if f != 1.0:
        for sg in x.get("segments", []) + ([x["total"]] if x.get("total") else []):
            for k in ("orders", "orders_prior", "orders_fy", "backlog", "backlog_prior", "backlog_fy"):
                if sg.get(k) is not None:
                    sg[k] *= f
    return x


PDF_DIR = Path.home() / "kessan-pdfs"                  # scripts/irbank_bulk.py が集めた過去の短信・説明資料


def past_pdf_text(session, code: str, did: str) -> str | None:
    """過去の短信 PDF の文字。手元(~/kessan-pdfs/{コード}/{書類番号}.pdf、読んだ文字は .txt に保存)を先に見て、
    無ければ IRBANK から取る。無い書類は None"""
    from pypdf import PdfReader
    doc = did[4:]
    pdf, txt = PDF_DIR / code / f"{doc}.pdf", PDF_DIR / code / f"{doc}.txt"
    if txt.exists():
        return txt.read_text(encoding="utf-8")
    if pdf.exists():
        content = pdf.read_bytes()
    else:
        resp = session.get(IRBANK_PDF.format(doc=doc))
        if resp is None:
            return None
        content = resp.content
    text = "\n".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(content)).pages)
    if pdf.parent.exists():
        try:
            txt.write_text(text, encoding="utf-8")
        except OSError:
            pass
    return text


def _pace_ok(x: dict, cur: dict, nq: int, cur_nq: int) -> bool:
    """本文から読んだ前の期の受注高が今回の累計と見合うか: 1四半期あたりの受注高が前の期の 1/3〜3倍。
    受注残高は 1/2〜2倍。今回の値が3か月の値・別の基準(個別など)だと崩れる(1951・8059)"""
    ct, xt = cur.get("total") or {}, x.get("total") or {}
    c, p = ct.get("orders"), xt.get("orders")
    if c is not None and p is not None:
        if p <= 0 or c <= 0:
            return False
        per = (c - p) / (cur_nq - nq) if cur_nq > nq else c / cur_nq
        return 1 / 3 <= per / (p / nq) <= 3
    c, p = ct.get("backlog"), xt.get("backlog")
    return c is not None and p is not None and p > 0 and 0.5 <= c / p <= 2


_SEC_ORD = re.compile(r"[①-⑩()（）0-9０-９.．、]*(受注高|受注実績|受注額|受注工事高)")
_SEC_BL = re.compile(r"[①-⑩()（）0-9０-９.．、]*(受注残高?|手持高|手持工事高|手持受注高|繰越高|次期繰越高|繰越工事高)")
_SEC_OTHER = re.compile(r"[①-⑩()（）0-9０-９.．、]*(売上高|売上実績|販売実績|生産実績|完成工事高|売上収益)")


def parse_pdf_totals(text: str, unit: float) -> dict | None:
    """PDF の受注高・受注残高の表の「合計」の行(「合 計 102,879 ( 100.0 ) 88,957 ( 100.0 ) △13,921 △13.5」)から全体の値を読む。
    値は括弧書き(構成比)を除いて「前期・当期・増減額(・増減率)」の並びで、当期−前期=増減額(±2)のときだけ当期として使う。
    区切りは見出しの行(縦書きで1字ずつの行も)で決め、予想・計画の区切りは読まない"""
    import unicodedata
    raw = [unicodedata.normalize("NFKC", l).strip() for l in text.splitlines()]
    raw = [l for l in raw if l]
    lines, i = [], 0
    while i < len(raw):                                # 縦書きの見出し(「受」「注」「高」)を1行にまとめる
        if len(raw[i]) == 1 and not raw[i].isdigit():
            j = i
            while j < len(raw) and len(raw[j]) == 1 and not raw[j].isdigit():
                j += 1
            lines.append("".join(raw[i:j]))
            i = j
        else:
            lines.append(raw[i])
            i += 1
    # 表の値が列ごとに縦に並ぶ PDF(タクミナ: 「合　　計」「（ うち海外 ）」「2,446」「(266)」「100.0」…)は、
    # 「合計」の行の後ろの数値だけの行を1行にまとめる
    merged, i = [], 0
    while i < len(lines):
        l = lines[i]
        if re.fullmatch(r"(合計|総合計|総計)", re.sub(r"\s", "", l)):
            j, toks = i + 1, []
            while j < len(lines) and j < i + 30:
                t = re.sub(r"\s", "", lines[j])
                if re.fullmatch(r"[(（]うち\S{1,6}[)）]", t):
                    j += 1
                    continue
                if not _PDF_TOKEN.match(t):
                    break
                toks.append(t)
                j += 1
            if len(toks) >= 3:
                vals = [t for t in toks if not re.match(r"[(（]", t)]
                if len(vals) >= 5:                     # 構成比の 100.0 を除く(金額・構成比・金額・構成比・増減額・増減率)
                    vals = [t for t in vals if t not in ("100.0", "100")]
                merged.append("合計 " + " ".join(vals))
                i = j
                continue
        merged.append(l)
        i += 1
    lines = merged
    found, sec = {}, None
    for l in lines:
        k = re.sub(r"\s", "", l)
        has_num = re.search(r"\d,\d{3}", l)
        if not has_num and len(k) <= 30:
            if re.search(r"予想|計画|見通し", k):
                sec = None
            elif _SEC_BL.match(k):
                sec = "backlog"
            elif _SEC_ORD.match(k):
                sec = "orders"
            elif _SEC_OTHER.match(k):
                sec = None
            continue
        if sec is None or sec in found:
            continue
        toks = [t for t in re.sub(r"[(（][^)）]*[)）]", " ", l).split()]
        n = 0
        while n < len(toks) and not _PDF_TOKEN.match(toks[n]):
            n += 1
        if not re.fullmatch(r"(合計|総合計|総計|連結合計|受注高合計|受注残高合計)", "".join(toks[:n])):
            continue
        vals = [_num(t) for t in toks[n:] if not re.fullmatch(r"[%％]", t)]
        if len(vals) >= 3 and all(v is not None for v in vals[:3]) and abs(vals[1] - vals[0] - vals[2]) <= 2 and vals[1] > 0:
            found[sec] = (vals[1], vals[0])
    if not found:
        return None
    total = {"name": "合計"}
    for m in METRICS:
        cur_v, prior_v = found.get(m, (None, None))
        total[m] = None if cur_v is None else cur_v * unit
        total[f"{m}_prior"] = None if prior_v is None else prior_v * unit
        total[f"{m}_fy"] = None
        total[f"{m}_yoy"] = None if not cur_v or not prior_v or prior_v <= 0 else round((cur_v / prior_v - 1) * 100, 1)
    return {"segments": [], "total": total}


def _from_fy_columns(cur: dict) -> dict | None:
    """1Q の受注の表に「前事業年度(通期)」の列があれば、その値で前期(通期)の記録を作る"""
    t = cur.get("total") or {}
    if t.get("orders_fy") is None and t.get("backlog_fy") is None:
        return None

    def fy(s):
        return {"name": s["name"], **{f"{m}{suf}": (s.get(f"{m}_fy") if suf == "" else None)
                                     for m in METRICS for suf in ("", "_prior", "_fy")},
                **{f"{m}_yoy": None for m in METRICS}}
    segs = [fy(s) for s in cur.get("segments", []) if s.get("orders_fy") is not None or s.get("backlog_fy") is not None]
    return {"segments": segs, "total": fy(t), "src": "cur_fy_column"}


_PERIOD_RE = re.compile(r"(前|当)(第[1-4１-４]四半期|中間|事業年度|連結会計年度|期)")


def parse_pdf_by_header(text: str, unit: float) -> dict | None:
    """PDF の受注の表を、表の見出しの行から列の意味を決めて読む(今回の表と列の並びが違う前の期の短信用)。
    「受注高 受注残 受注高 受注残 受注高 受注残」の行と、その上の期間の見出し(「前第３四半期累計期間」
    「当第３四半期累計期間」「前事業年度」、サムコ)から、列ごとの期間(当期・前年・前期末)と項目を決める"""
    import unicodedata
    lines = [unicodedata.normalize("NFKC", _undup(l)).strip() for l in text.splitlines()]
    lines = [l for l in lines if l]
    for i, l in enumerate(lines):
        toks = l.split()
        if len(toks) < 2 or not all(_metric(t) for t in toks):
            continue
        metrics = [_metric(t) for t in toks]
        periods = []
        for j in range(i - 1, max(i - 15, -1), -1):
            if re.search(r"\d,\d{3}", lines[j]):
                break
            for m in reversed(list(_PERIOD_RE.finditer(re.sub(r"\s", "", lines[j])))):
                periods.insert(0, m.group(0))
        dedup = []
        for p in periods:
            if not dedup or dedup[-1] != p:
                dedup.append(p)
        periods = dedup
        if not periods or len(metrics) % len(periods):
            continue
        k = len(metrics) // len(periods)
        quarterly = any(re.match(r"当(第|中間)", p) for p in periods)

        def kind(p):
            if p.startswith("当"):
                return "cur"
            if re.match(r"前(第|中間)", p) or not quarterly:
                return "prior"
            return "fy"
        cols = [(kind(periods[j // k]), metrics[j]) for j in range(len(metrics))]
        if [c for c in cols if c[0] == "cur"] == []:
            continue
        facts = []
        for l2 in lines[i + 1:i + 40]:
            t2 = [t for t in l2.split() if not re.match(r"^[（(]", t)]
            n = len(t2)
            while n > 0 and _PDF_TOKEN.match(t2[n - 1]):
                n -= 1
            vals = t2[n:]
            if not vals:
                if facts:
                    break
                continue
            if len(vals) != len(cols) or n == 0:
                if facts:
                    break
                continue
            seg = "".join(t2[:n])
            for (per, met), v in zip(cols, vals):
                x = _num(v)
                if x is not None:
                    facts.append({"seg": seg, "metric": met, "period": per, "kind": "value", "value": x,
                                  "signed": _signed(v), "growth_label": False, "group": False, "parent": None})
        if facts:
            x = _assemble(facts, unit)
            if x:
                return x
    return None


def _fy_from_pdf(text: str, cur: dict) -> dict | None:
    """通期の短信 PDF から、今回(1Q)の表の「前事業年度」の列と同じ値の行を探して読む(マツモト: 通期の短信の表は
    「前期 受注高 受注残高 | 当期 受注高 受注残高」で今回の表と形が違う)。合計の行で前事業年度の受注高・受注残高が
    並ぶ位置を見つけ、セグメントの行も同じ位置の値を使う"""
    t, unit = cur.get("total") or {}, cur.get("unit") or 1e6
    want = [(m, t.get(f"{m}_fy")) for m in METRICS if t.get(f"{m}_fy") is not None]
    if not want:
        return None
    lines = [_undup(l) for l in text.splitlines() if l.strip()]

    def split(l):
        toks = l.split()
        k = len(toks)
        while k > 0 and _PDF_TOKEN.match(toks[k - 1]):
            k -= 1
        return re.sub(r"\s", "", "".join(toks[:k])), [_num(x) for x in toks[k:]]
    pos = None
    for l in lines:
        lab, vals = split(l)
        if not vals or not re.search(r"計$", lab):
            continue
        p = [next((i for i, v in enumerate(vals) if v is not None and abs(v * unit - w) < unit / 2), None) for _, w in want]
        if all(i is not None for i in p):
            pos, n = dict(zip([m for m, _ in want], p)), len(vals)
            break
    if pos is None:
        return None
    # 今回の表に前事業年度の列が無い項目(186A の受注総額)は、本文の値(百万円)と合計の行の値が合えばその位置
    narr = (parse_narrative_text(text, []) or {}).get("total") or {}
    row = next(v for lab, v in map(split, lines) if v and re.search(r"計$", lab) and len(v) == n and
               all(v[i] is not None and abs(v[i] * unit - w) < unit / 2 for (_, w), i in zip(want, pos.values())))
    for m in METRICS:
        if m not in pos and narr.get(m):
            hit = [i for i, v in enumerate(row) if v is not None and i not in pos.values() and abs(v * unit - narr[m]) < 1e6]
            if len(hit) == 1:
                pos[m] = hit[0]

    def rec(name, vals):
        return {"name": name, **{f"{m}{suf}": (vals[pos[m]] * unit if suf == "" and m in pos and vals[pos[m]] is not None else None)
                                 for m in METRICS for suf in ("", "_prior", "_fy")},
                **{f"{m}_yoy": None for m in METRICS}}
    segs = []
    for sg in cur.get("segments", []):
        key = re.sub(r"\s", "", sg["name"])
        row = next((v for lab, v in map(split, lines) if lab == key and len(v) == n and all(v[i] is not None for i in pos.values())), None)
        if row is None:
            return None                                # 1つでも読めなければ使わない
        segs.append(rec(sg["name"], row))
    total = rec(t.get("name") or "合計", row)
    for m, w in want:
        total[m] = w
    return {"segments": segs, "total": total, "src": "fy_pdf_columns"}


def _read_past_pdf(text: str, cur: dict) -> dict | None:
    """前の期の短信 PDF を今回と同じやり方で読む: 表の会社は表の形(tpl)で、本文の文から読んだ会社は本文から"""
    if cur.get("src") == "narrative":
        x = parse_narrative_text(text, [s["name"] for s in cur.get("segments", [])])
        if x and cur.get("segments") and not x["segments"]:
            return None                                # セグメントの文が見つからない期は使わない
        return x
    x = parse_pdf_text(text, cur["tpl"], cur.get("unit"))
    if x:
        return x
    # 表の見出しの行から列の意味を決めて読む(サムコ: 前の期の表は「前3Q | 当3Q | 前事業年度」、今回は「前期 | 当期」)。
    # 今回の記録のセグメントの名前が半分以上合うときだけ
    y = parse_pdf_by_header(text, cur.get("unit") or 1e6)
    names = {re.sub(r"\s", "", sg["name"]) for sg in cur.get("segments", [])}
    if y and names and len(names & {re.sub(r"\s", "", sg["name"]) for sg in y["segments"]}) * 2 >= len(names):
        y["src"] = "header_table_fallback"
        return y
    # 表の形が違っても、受注高・受注残高の区切りの「合計」の行(前期・当期・増減)は読める(東亜建設の縦書きの見出しの表)
    y = parse_pdf_totals(text, cur.get("unit") or 1e6)
    ct = cur.get("total") or {}
    if y and any(ct.get(f) is not None and y["total"].get(f) is not None for f in ("orders", "backlog")):
        y["src"] = "table_total_fallback"
        return y
    # 表の形が前の期と違って読めないときは、本文の「受注高は○○百万円」から(1762 髙松・1780 ヤマウラなど)。
    # 今回の記録にある値(受注高・受注残高)の合計が読めたときだけ使う
    y = parse_narrative_text(text, [s["name"] for s in cur.get("segments", [])])
    ct, yt = cur.get("total") or {}, (y or {}).get("total") or {}
    if not any(yt.get(f) is not None for f in ("orders", "backlog")):
        # セグメント名(「その他」など)の短い行が本文より前にあると全体の文を読む範囲が無くなる(ショーボンド): 全体だけ読む
        y = parse_narrative_text(text, [])
        yt = (y or {}).get("total") or {}
    if y and any(ct.get(f) is not None and yt.get(f) is not None for f in ("orders", "backlog")):
        y["src"] = "narrative_fallback"
        return y
    return None


def backfill_history(cfg: dict, depth: int = 20, data_dir: Path = DATA_DIR,
                     session: RateLimitedSession | None = None) -> dict:
    """受注の表がある会社について、過去 depth 四半期(既定5年)の決算短信 PDF を IRBANK から読み、
    受注の推移(期ごとの累計・3か月・受注残)を出せるようにする。期末は短信の表題から決める。手元でのみ実行。"""
    from pypdf import PdfReader
    from .datasources import irbank

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
        if (d.get("tpl") or d.get("src") == "narrative") and r["period_end"]:
            latest[r["code"]] = {**r, "data": d}
    rows, stats = [], {"codes": 0, "pdfs": 0, "ok": 0}
    for code, cur in latest.items():
        end = str(cur["period_end"])[:7]
        floor = _ym_shift(end, -3 * depth)
        try:
            items = [(i, q, ym) for i, q, ym in _irbank_earnings(session, code, fresh_after=str(cur["disclosed_at"])[:10])
                     if ym and floor <= ym < end and (code, ym) not in have]
        except Exception as e:
            log.warning("受注の推移: IRBANK 一覧の取得失敗 %s: %s", code, e)
            continue
        stats["codes"] += 1
        seen = set()
        for did, q, ym in items:                       # 同じ期の短信が複数あれば新しい方だけ
            if ym in seen:
                continue
            seen.add(ym)
            x = None
            try:
                text = past_pdf_text(session, code, did)
                stats["pdfs"] += 1
                if text is not None:
                    x = _read_past_pdf(text, cur["data"])
                    if x:
                        x = fix_scale(code, ym, q, x, hist)
                        x = fix_scale_to(x, cur["data"].get("total") or {})
            except Exception as e:
                log.warning("受注の推移: PDF の読み取り失敗 %s %s: %s", code, did, e)
            if x:
                x["src"] = "irbank_pdf"
                stats["ok"] += 1
            rows.append({"disclosure_id": did, "code": code,
                         "disclosed_at": f"{did[4:8]}-{did[8:10]}-{did[10:12]}T00:00",
                         "n_q": str(q), "period_end": ym, "status": "ok" if x else "none",
                         "data": json.dumps(x, ensure_ascii=False) if x else ""})
    if rows:
        new = pd.DataFrame(rows).reindex(columns=COLUMNS)
        out = pd.concat([df[~df["disclosure_id"].isin(new["disclosure_id"])], new])
        out.sort_values(["disclosed_at", "disclosure_id"], ascending=False).to_csv(path(data_dir), index=False,
                                                                                 encoding="utf-8")
    log.info("orders history: %s", stats)
    return stats


def backfill(cfg: dict, data_dir: Path = DATA_DIR, session: RateLimitedSession | None = None) -> dict:
    """受注の表がある会社で、前の四半期(QoQ に要る分)の記録が無ければ、IRBANK が保管している
    前の期の決算短信 PDF を今回と同じ表の形で読んで補う。TDnet は約1か月で消えるため。
    IRBANK は GitHub Actions からは使えないので手元で実行する。"""
    from pypdf import PdfReader
    from .datasources import irbank

    session = session or irbank.session_from_config(cfg)
    df = load(data_dir)
    arch = {a["disclosure_id"]: a for a in earnings.load_archive(data_dir).to_dict("records")}
    # 前に読めなかった(none)過去の期は、保存した PDF を毎回今の読み方で読み直す(だめなら none_retry)
    retry = {(r["code"], str(r["period_end"])[:7]): r["disclosure_id"] for r in df.to_dict("records")
             if r["period_end"] and r["status"] in ("none", "none_retry") and str(r["disclosed_at"]).endswith("T00:00")}
    have = {(r["code"], str(r["period_end"])[:7]) for r in df.to_dict("records") if r["period_end"]} - set(retry)
    latest: dict[str, dict] = {}
    for r in df[df["status"] == "ok"].sort_values("disclosed_at").to_dict("records"):
        a = arch.get(r["disclosure_id"])
        if a and not r["period_end"]:
            r["n_q"], r["period_end"] = a["n_q"], a["period_end"]
        d = json.loads(r["data"])
        if (d.get("tpl") or d.get("src") == "narrative") and r["period_end"] and r["n_q"]:
            latest[r["code"]] = {**r, "data": d}

    rows, stats = [], {"codes": 0, "pdfs": 0, "ok": 0}
    for code, cur in latest.items():
        n_q, end = int(float(cur["n_q"])), str(cur["period_end"])[:7]
        # 受注残の QoQ は1つ前、受注高(3か月単独)の QoQ は2Q以外は2つ前まで要る
        back = 1 if n_q == 2 else 2
        need = [(k, _ym_shift(end, -3 * k), (n_q - k - 1) % 4 + 1) for k in range(1, back + 1)]
        need = [x for x in need if (code, x[1]) not in have]
        if not need:
            continue
        stats["codes"] += 1
        try:
            # 今回より前の1年以内の短信
            floor = f"1401{int(cur['disclosure_id'][4:8]) - 1}{cur['disclosure_id'][8:]}"
            ids = [x for x in _irbank_earnings_ids(session, code, fresh_after=str(cur["disclosed_at"])[:10]) if floor < x[0] < cur["disclosure_id"]]
        except Exception as e:
            log.warning("受注の補完: IRBANK 一覧の取得失敗 %s: %s", code, e)
            continue
        for k, ym, nq in need:
            did = retry.get((code, ym)) or next((i for i, q in ids if q == nq), None)  # その四半期の一番新しい短信
            if did is None:
                continue
            x, text = None, None
            try:
                text = past_pdf_text(session, code, did)
                stats["pdfs"] += 1
                if text is not None:
                    x = _read_past_pdf(text, cur["data"])
                    if x and x.get("src") in ("narrative_fallback", "table_total_fallback", "header_table_fallback") and not _pace_ok(x, cur["data"], nq, n_q):
                        log.info("受注の補完: 本文の値が今回と合わないので捨てる %s %s", code, ym)
                        x = None
            except Exception as e:
                log.warning("受注の補完: PDF の読み取り失敗 %s %s: %s", code, did, e)
            if x:
                x["src"] = "irbank_pdf"
            elif k == 1 and nq == 4:
                # 今回(1Q)の表の「前事業年度」の列 = 前期(通期)の値(マツモト)。通期の短信 PDF の同じ値の位置から読み、
                # だめなら今回の表の値
                x = (text and _fy_from_pdf(text, cur["data"])) or _from_fy_columns(cur["data"])
            if x:
                stats["ok"] += 1
            rows.append({"disclosure_id": did, "code": code,
                         "disclosed_at": f"{did[4:8]}-{did[8:10]}-{did[10:12]}T00:00",
                         "n_q": str(nq), "period_end": ym,
                         "status": "ok" if x else ("none_retry" if (code, ym) in retry else "none"),
                         "data": json.dumps(x, ensure_ascii=False) if x else ""})
    if rows:
        new = pd.DataFrame(rows).reindex(columns=COLUMNS)
        out = pd.concat([df[~df["disclosure_id"].isin(new["disclosure_id"])], new])
        out.sort_values(["disclosed_at", "disclosure_id"], ascending=False).to_csv(path(data_dir), index=False,
                                                                                 encoding="utf-8")
    log.info("orders backfill: %s", stats)
    return stats


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


_N_UNITS = {"百万円": 1e6, "千円": 1e3, "億円": 1e8}
_N_NUM = r"(△|▲|-)?([\d,]+(?:\.\d+)?)(百万円|千円|億円)"
_N_YOY = r"(?:前年同期比|前年同四半期比|前年同中間期比|前期比|同)([\d.]+)%(増加|減少|増|減)"


def _narr_value(text: str, words: str) -> tuple[float | None, float | None]:
    """「受注高は31,796百万円(前年同期比52.2%増加)」「受注高は前年同期比50.4%増加の37,270百万円」→ (値(円), 増減率)。
    最初に出てくる文(内訳の文より前)を使う"""
    # 「受注高につきましては、大型データセンター案件を中心に…、92,739百万円(前年同期比100.3%増)」のように
    # 間に説明が入る文。間に金額・売上・利益・文の終わりがあれば別の話なので読まない
    # 「受注残高は、受注高が売上高を下回った結果、…」の「売上高を上回/下回」は話の続き(ショーボンド)
    gap = r"(?:(?!売上(?!高を[上下]回)|利益|損失|百万円|億円|千円|%|。).){1,120}?"
    # 「受注高は、前年同期比2,470百万円減少(13.0%減)の16,582百万円」(増減額と率のあとに金額)
    diff = r"(?:前年同期比|前年同四半期比|前年同中間期比|前期比)?[\d,.]+(?:百万円|億円|千円)(?:増加|減少)\(([\d.]+)%(増|減)\)の"
    # 「前年同期比1,167百万円(5.0%)増加の24,359百万円」
    diff2 = r"(?:前年同期比|前年同四半期比|前年同中間期比)[\d,.]+(?:百万円|億円|千円)\(([\d.]+)%\)(増加|減少)の"
    # 説明をはさむ文の金額が増減額(「前年同期比1,167百万円…増加」「期首比3,432百万円増の」)なら読まない
    not_diff = r"(?<!比)(?<![^に]より)(?<![\d,，.])"   # 「前年同期より」は読まない、「…により12,136百万円」は読む
    after = r"(?![増減]|\([\d.]+%\)[増減])"
    # 「受注残高は、2025年12月末の2,400億円から2026年９月末には3,280億円へ」: 「から」のあとの金額(DMG森精機)
    frm = r"[^。]{0,30}?[\d,.]+(?:百万円|億円|千円)から[^。]{0,25}?(?<![\d,.])"
    pats = [(words + r"(?:は|が|につきましては)、?" + frm + _N_NUM + r"(?:へ|に)", "snu"),
            (words + r"(?:は|が|につきましては)?、?" + _N_NUM + after + r"(?:\(" + _N_YOY + r")?", "snuyd"),
            # 「約286,000千円の受注残が発生」(北の達人)
            (r"約?" + _N_NUM + r"の" + words + r"(?:が|は)", "snu"),
            (words + r"(?:は|が)、?" + _N_YOY + r"の" + _N_NUM, "ydsnu"),
            (words + r"(?:は|が|につきましては)、?(?:" + gap + r")?" + diff + _N_NUM, "ydsnu"),
            (words + r"(?:は|が|につきましては)、?(?:" + gap + r")?" + diff2 + _N_NUM, "ydsnu"),
            (words + r"(?:は|が|につきましては)、?" + gap + not_diff + _N_NUM + after + r"(?:\(" + _N_YOY + r")?", "snuyd")]
    best = None
    for pat, order in pats:
        m = re.search(pat, text)
        if m and (best is None or m.start() < best[0].start()):
            best = (m, order)
    if not best:
        return None, None
    m, order = best
    g = dict(zip(order, m.groups()))
    sign, num, unit, yv, yd = g["s"], g["n"], g["u"], g.get("y"), g.get("d")
    v = float(num.replace(",", "")) * _N_UNITS[unit] * (-1 if sign else 1)
    y = None if yv is None else float(yv) * (-1 if yd.startswith("減") else 1)
    return v, y


_KANJI_DIGIT = r"(?:\d+千)?(?:\d+百)?(?:\d+十)?\d*"
_KANJI_AMT = re.compile(r"(\d[\d,]*)億(" + _KANJI_DIGIT + r")万円|(\d[\d,]*)億円|(?<![\d,億])((?:\d+千)(?:\d+百)?(?:\d+十)?\d*|\d+百(?:\d+十)?\d*)万円")


def _man_part(s: str) -> int:
    """「6千6百」→ 6600(万)。数字だけなら そのまま"""
    n = 0
    for v, u in re.findall(r"(\d+)([千百十]?)", s):
        n += int(v) * {"千": 1000, "百": 100, "十": 10, "": 1}[u]
    return n


def _kanji_amounts(t: str) -> str:
    """「800億6千6百万円」「299億3千万円」「9千万円」「2.2倍」を表の書き方(「80,066百万円」「120.0%増」)に直す"""
    def amt(m):
        if m.group(1) is not None:
            man = int(m.group(1).replace(",", "")) * 10000 + _man_part(m.group(2))
        elif m.group(3) is not None:
            man = int(m.group(3).replace(",", "")) * 10000
        else:
            man = _man_part(m.group(4))
        v = man / 100                                  # 百万円
        return (f"{v:,.0f}" if v == int(v) else f"{v:,.2f}".rstrip("0")) + "百万円"
    t = _KANJI_AMT.sub(amt, t)
    return re.sub(r"(前年同期比|前期比|同)([\d.]+)倍", lambda m: f"{m.group(1)}{(float(m.group(2)) - 1) * 100:.1f}%増", t)


def parse_narrative(html: str, names: list[str]) -> dict | None:
    """受注の表が無い短信の本文から、全体とセグメントごとの受注高・受注残高(繰越工事高)を読む。"""
    return parse_narrative_text(BeautifulSoup(html, "html.parser").get_text("\n"), names)


def parse_narrative_text(text: str, names: list[str]) -> dict | None:
    """短信の本文の文字(HTML または PDF)から受注高・受注残高を読む。
    セグメントは見出し(セグメント名だけの短い行)から次の見出しまで、全体は最初の見出しより前。"""
    import unicodedata
    lines = [_kanji_amounts(unicodedata.normalize("NFKC", re.sub(r"\s", "", l))) for l in text.split("\n")]
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
    ORD, BL = r"受注(?:高|額|実績)", r"(?:受注残高?|繰越工事高|手持受注高?)"

    def read(block):
        o, oy = _narr_value(block, ORD)
        b, by = _narr_value(block, BL)
        return {"orders": o, "orders_yoy": oy, "backlog": b, "backlog_yoy": by, "backlog_fy": None}

    first = heads[0][0] if heads else len(lines)
    total = {"name": "合計", **read("".join(lines[:first]))}
    segs, seen = [], set()
    for h, (i, n) in enumerate(heads):
        if n in seen:
            continue
        end = heads[h + 1][0] if h + 1 < len(heads) else min(i + 40, len(lines))
        x = read("".join(lines[i + 1:end]))
        if x["orders"] is not None or x["backlog"] is not None:
            segs.append({"name": n, **x})
            seen.add(n)
    if not segs and total["orders"] is None and total["backlog"] is None:
        return None
    return {"total": total, "segments": segs, "src": "narrative"}


def parse_zip(zip_bytes: bytes, cum_sales: float | None = None) -> dict | None:
    try:
        z = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile:
        return None
    names = [n for n in z.namelist() if n.endswith("qualitative.htm")]
    if not names:
        return None
    html = z.read(names[0]).decode("utf-8", errors="replace")
    if not _TABLE_HINT.search(html):                  # 受注の言葉が無ければ表を探さない(大半の短信)
        return None
    x = parse_qualitative(html, cum_sales)
    if x:
        return x
    # 受注の表が無い短信は本文の文(「受注高は X 百万円(前年同期比 Y% 増)」)から。セグメント名は短信のセグメント情報
    from . import segments
    sg = segments.parse_zip(zip_bytes)
    return parse_narrative(html, [g["name"] for g in sg["segments"]] if sg else [])


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
            content = tdnet.get_zip(session, r.xbrl_url)   # 決算の数値を読んだときの zip を使い回す
        except Exception as e:                         # 次回再試行
            log.warning("受注: XBRL取得失敗 %s: %s", r.code, e)
            continue
        if content is None:
            continue
        sales = pd.to_numeric(a.get("cum_sales"), errors="coerce")
        try:
            x = parse_zip(content, None if pd.isna(sales) else float(sales))
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


def drop_inconsistent_narrative(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """本文の文から読んだ受注(src=narrative)は「期首からの累計」として扱うので、同じ年度の中で受注高が減る会社は
    文の数値が3か月の値(例: 「受注高:1,604百万円 …(3Q)」)とみなし、その会社の文から読んだ記録を使わない(status=bad)"""
    ok = df[df["status"] == "ok"]
    latest = ok.sort_values("disclosed_at").groupby("code").tail(1)
    narr = set(latest[latest["data"].str.contains('"src": "narrative"')]["code"])   # 一番新しい記録が本文の文から
    ok = ok[ok["code"].isin(narr)]
    bad_codes = []
    for code, g in ok.groupby("code"):
        recs = sorted(((str(r["period_end"])[:7], int(float(r["n_q"] or 0)), json.loads(r["data"])["total"].get("orders"))
                       for r in g.to_dict("records")), key=lambda t: t[0])
        prev = {}
        for ym, nq, v in recs:
            p = prev.get(_ym_shift(ym, -3))
            if v is not None and p and p[0] == nq - 1 and p[1] is not None and v < p[1] - 1e6:
                bad_codes.append(code)
                break
            prev[ym] = (nq, v)
    m = df["code"].isin(bad_codes) & (df["status"] == "ok")      # その会社の文から読んだ記録(過去の PDF の分も)
    df = df.copy()
    df.loc[m, "status"], df.loc[m, "data"] = "bad", ""
    return df, bad_codes
