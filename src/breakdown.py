"""決算短信の補足情報にある「品目別実績」「製品別売上高」「部門別売上高」などの内訳の表(売上高)を読む → data/breakdowns.csv。

セグメントが1つの会社(トーメンデバイスの メモリ / システムLSI など)は、セグメント情報の代わりにこの表で中身がわかる。
読むのは、合計の行の当期の金額が、その期の連結売上(短信1ページ目の累計)と単位の丸めの範囲で一致する表だけ
(=売上の内訳で、期間も同じだと確かめられる)。過去の期は手元の過去の短信 PDF を同じ形で読む(合計を同じく照合)。
"""
import io
import json
import logging
import re
import unicodedata
import zipfile
from pathlib import Path

import pandas as pd
from bs4 import BeautifulSoup

from . import disclosures, earnings, history
from .config import DATA_DIR
from .datasources import tdnet
from .datasources.http import RateLimitedSession

log = logging.getLogger(__name__)

COLUMNS = ["disclosure_id", "code", "disclosed_at", "n_q", "period_end", "status", "data"]
KINDS = r"品目別|製品別|商品別|部門別|品種別|用途別|分野別|事業別|サービス別|業態別|ブランド別|地域別|仕向地別"
_UNITS = {"百万円": 1e6, "千円": 1e3, "億円": 1e8}
QUAL_DIR = Path.home() / "kessan-pdfs" / "qual"            # 手元でのみ: 短信の補足情報(qualitative.htm)の保存先


def path(data_dir: Path = DATA_DIR) -> Path:
    return data_dir / "breakdowns.csv"


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


def segment_names(data_dir: Path = DATA_DIR) -> dict[str, list[str]]:
    """開示ごとのセグメント名(セグメント情報と同じ表を内訳として出さないため)"""
    from . import segments
    df = segments.load(data_dir)
    out = {}
    for r in df[df["status"] == "ok"].to_dict("records"):
        try:
            out[r["disclosure_id"]] = [x["name"] for x in json.loads(r["data"]).get("segments", [])]
        except (ValueError, TypeError):
            pass
    return out


def _key(s: str) -> str:
    return re.sub(r"\s", "", unicodedata.normalize("NFKC", s or ""))


def _num(t: str) -> float | None:
    from .orders import _num as n
    return n(t or "")


_PL = r"売上原価|売上総利益|販売費及び一般管理費|営業外|経常利益|特別利益|法人税"
_TOTAL = re.compile(r"(合計|総合計|計|総計|連結合計|(連結)?(売上高|営業収益|売上収益)(合計|計)?)")


def _is_total(r: list[str]) -> bool:
    return any(_TOTAL.fullmatch(_key(c)) for c in r if c and _num(c) is None)


# 品目ではない行(収益の分解の表の「外部顧客への売上高」「セグメント間の内部売上高」、収益認識の時期の区分など)
_META = re.compile(r"外部顧客|セグメント間|内部売上|振替高|顧客との契約から生じる収益|その他の収益|一時点|一定の期間"
                   r"|消去|調整|割戻|控除|配分していない")
_DASH = re.compile(r"^[-―－—‐]+$")


def _item_name(r: list[str], seen: set[str]) -> str:
    """行の品目名: 文字のセルのうち一番右(「家庭電化商品 | エアコン」→ エアコン)。
    大分類の列があれば大分類を付ける(「小計」「計」だけなら大分類と合わせる)"""
    first = next((j for j, c in enumerate(r) if c and _num(c) is not None), len(r))   # 数値より左(右の説明の列は使わない)
    t = list(dict.fromkeys(_key(c) for c in r[:first] if c and _num(c) is None and not _DASH.match(_key(c))))
    if not t:
        return ""
    name = t[-1]
    if len(t) > 1:                                     # 大分類の列がある表は「大分類・品目」(宝HD: 宝酒造・焼酎)
        name = t[-2] + name if re.fullmatch(r"小計|計|合計", name) else f"{t[-2]}・{name}"
    return name


def _check_sum(items: list[dict], total: float, unit: float) -> bool:
    """品目を足すと合計になるか。小計の行(下の品目の合計=見出し、上の品目の合計=小計)を見つけて sub にしてから足す"""
    def close(a, b, k):
        return abs(a - b) <= unit * max(1, k) + 1e-6 * abs(b)          # 表示単位未満の切り捨て・四捨五入の分

    def leaves():
        return [i for i in items if not i["sub"] and i["sales"] is not None]

    def ok():
        lv = leaves()
        return close(sum(i["sales"] for i in lv), total, len(lv))

    if ok():
        return True
    for i in items:                                     # 「計」と名前で分かる小計の印を一度外して数で決め直す
        i["sub"] = False
    changed = True
    while changed and not ok():
        changed = False
        idx = [k for k, i in enumerate(items) if not i["sub"] and i["sales"] is not None]
        for pos, k in enumerate(idx):
            v = items[k]["sales"]
            for run in (idx[pos + 1:], idx[:pos][::-1]):  # 下に続く品目(見出し)/ 上の品目(小計)
                s = 0.0
                for n, j in enumerate(run, 1):
                    s += items[j]["sales"]
                    if n >= 2 and close(s, v, n):
                        items[k]["sub"] = True
                        changed = True
                        break
                    if abs(s) > abs(v) * 1.5 + unit * n:
                        break
                if items[k]["sub"]:
                    break
            if changed:
                break
    return ok()


def parse_html(html: str, cum_sales: float | None, seg_names=()) -> dict | None:
    """qualitative.htm の内訳の表。cum_sales(連結の累計売上、円)と合計が合う表を返す(品目別などの見出しがある表を優先、
    名前の半分以上がセグメント名(seg_names)と同じ表はセグメント情報なので使わない):
    {"title", "unit", "items": [{name, sales, sales_prior, sub}], "total": {...}, "tpl": {n, cur, prior, names}}"""
    from .orders import _grid
    if not cum_sales:
        return None
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n")
    segs = {_key(n) for n in seg_names}
    found = []
    for t in soup.find_all("table"):
        g = _grid(t)
        data = [r for r in g if r and any(_num(c) is not None for c in r[1:])]
        if len(data) < 3:
            continue
        head = g[:g.index(data[0])]
        width = max(len(r) for r in g)
        cols = [_key(" ".join(dict.fromkeys(r[j] for r in head if j < len(r) and r[j]))) for j in range(width)]
        label = lambda r: _key(next((c for c in r if c and _num(c) is None), ""))
        # 当期の金額の列: 見出しに「当」(当中間・当第N四半期・当連結会計年度・当期)、構成比・増減ではない列
        num_cols = sorted({j for r in data for j, c in enumerate(r) if j > 0 and _num(c) is not None})
        def amount(j, cur):
            h = cols[j]
            if re.search(r"構成比|増減|前年比|比率|%|％", h):
                return False
            return bool(re.search(r"当(中間|第|連結|期|事業|四半期)", h)) if cur else \
                bool(re.search(r"前(中間|第|同|年同)", h)) and not re.search(r"前(連結会計年度|事業年度|期末)", h)
        cur = [j for j in num_cols if amount(j, True)]
        prior = [j for j in num_cols if amount(j, False)]
        if not cur:                                    # 当期の金額の列が1つだけの表(「売上高(百万円)」と前年比など)
            cur = [j for j in num_cols if re.search(r"円", cols[j]) and not re.search(r"構成比|増減|前年比|比率|%|％", cols[j])
                   and not re.search(r"前(中間|第|同|年|期|連結|事業)", cols[j])]
            if len(cur) != 1:
                continue
        # 単位(見出しや表の上の文)
        mu = re.findall(r"(百万円|千円|億円)", " ".join(cols)) or re.findall(r"単位\s*[:：]\s*(百万円|千円|億円)", text)
        unit = _UNITS[mu[0]] if mu else 1e6
        c0 = cur[0]
        # 合計の行: 当期の金額が連結売上と単位の丸めの範囲で一致する行(合わない表は内訳ではない)
        tot = next((r for r in data if c0 < len(r) and _is_total(r) and _num(r[c0]) is not None
                    and abs(_num(r[c0]) * unit - cum_sales) <= max(unit, abs(cum_sales) * 0.001)), None)
        if tot is None:
            continue
        tv = _num(tot[c0])
        p0 = prior[0] if prior else None
        # 表の名前: 表のすぐ上の見出し(「品目別実績」「製品別売上高」など)
        before = _key("".join(reversed([str(x) for x in t.find_all_previous(string=True, limit=15)])))
        ms = list(re.finditer(r"(" + KINDS + r")(実績|売上高?|売上収益|販売実績|の状況)?", before))
        title = ms[-1].group(0) if ms else ("収益の分解" if "収益" in before[-80:] and "分解" in before[-80:] else "売上の内訳")
        if any(re.search(_PL, label(r)) for r in data):
            continue                                   # 損益計算書(売上原価・販管費…)は内訳ではない
        items = []
        for r in data[:data.index(tot)]:               # 合計の行より上だけ
            name = _item_name(r, {i["name"] for i in items})
            if not name:
                continue
            v = _num(r[c0]) if c0 < len(r) else None
            pv = _num(r[p0]) if p0 is not None and p0 < len(r) else None
            items.append({"name": name, "sales": None if v is None else v * unit,
                          "sales_prior": None if pv is None else pv * unit, "sub": bool(re.search(r"小計|計$", name)),
                          "adj": bool(_META.search(name))})
        if not _check_sum(items, tv * unit, unit) or sum(not i["sub"] and not i["adj"] for i in items) < 2:
            continue                                   # 品目を足して合計にならない表・品目が2つ未満の表は読まない
        tp = _num(tot[p0]) if p0 is not None and p0 < len(tot) else None
        if segs and sum(i["name"] in segs for i in items) * 2 >= len(items):
            continue
        found.append({"title": re.sub(r"[（()）]", "", title), "unit": unit, "items": items,
                      "total": {"sales": tv * unit, "sales_prior": None if tp is None else tp * unit},
                      "tpl": {"n": len(num_cols), "cur": num_cols.index(c0),
                              "prior": None if p0 is None else num_cols.index(p0),
                              "names": [i["name"] for i in items], "unit": unit}})
    named = [f for f in found if re.search(KINDS, f["title"])]
    return (named or found or [None])[0]


def parse_pdf_text(text: str, tpl: dict, cum_sales: float | None) -> dict | None:
    """過去の短信 PDF の文字を、今回の表と同じ形(品目名の並び・数値の列の数と位置)で読む。
    合計が連結売上と合い、品目を足して合計になるときだけ"""
    if not cum_sales:
        return None
    lines = _restack([unicodedata.normalize("NFKC", l).strip() for l in text.splitlines() if l.strip()])
    num = re.compile(r"^[△▲\-+]?[\d,]+(\.\d+)?%?$|^[-―－]$")      # 「+1.4」(前年比)も数値
    rows = []
    norm = lambda n: re.sub(r"[ー・]", "", n)
    bases = [norm(_key(n.split("・")[-1])) for n in tpl["names"]]
    pend = []                                          # 直前の文字だけの行(折り返した品目名)
    for l in lines:
        # 「6,311 6.1 ％」のように % が離れている行(1972)は数値に付ける
        toks = re.sub(r"(\d)\s+%(?=\s|$)", r"\1%", re.sub(r"([△▲+])\s+(\d)", r"\1\2", l)).split()
        # 品目名(左の文字)+ 続く数値(右に説明の文があれば数値の後ろは使わない)
        # 品目名の中の「-」(「Ｒ － Ｔ 238」)は数値の空欄ではない: 次が数値でない「-」は名前の一部
        if toks and num.match(toks[0]) and not re.fullmatch(r"[-―－]", toks[0]):
            k = 0                                      # 数値だけの行(品目名は上の行、2733)
        else:
            k = next((i for i, t in enumerate(toks) if i > 0 and num.match(t)
                      and not (re.fullmatch(r"[-―－]", t) and i + 1 < len(toks) and not num.match(toks[i + 1]))), len(toks))
        e = k
        while e < len(toks) and num.match(toks[e]):
            e += 1
        name, vals = _key("".join(toks[:k])), [t.rstrip("%") for t in toks[k:e]]
        if not vals:
            pend = (pend + [l])[-4:]
            continue
        # 品目名が上の行から折り返している(「ストレージ・ネットワーク」「機器等 ― 5,583 …」、2760)、品目名と値が別の行
        # (「ペット ペット用品 48,202」の上の「ヘルス＆ビューティー」「（Health & Beauty）」「化粧品、…」)
        if not any(norm(name).endswith(b) for b in bases if b):
            for cut in range(len(pend)):
                cand = _key("".join(pend[cut:])) + name
                if any(b and norm(cand).endswith(b) for b in bases):
                    name = cand
                    break
            else:
                # 上の行が「・」で終わる(「園芸・農業・」「ペット用品 44,763」、8218)のは名前の折り返し
                if pend and name and re.search(r"[・、]$", _key(pend[-1])):
                    name = _key(pend[-1]) + name
        pend = []
        if vals and name:                              # 金額の列が1つだけの表もある(テノックス・北の達人の1Q)
            rows.append((name, vals))
    unit, cur, prior = tpl["unit"], tpl["cur"], tpl.get("prior")

    def match(t, u):
        # 連結売上(四半期の履歴)は百万円で丸めた値の会社が多いので、千円の表でも百万円までの差は同じ値とみる(横田製作所)
        x = _num(t)
        return x is not None and abs(x * u - cum_sales) <= max(u, 1e6, abs(cum_sales) * 0.001)
    # 通期の短信は「前期(通期)」の列が無いなど列の数が違うことがあるので、当期・前期の列の位置が収まる行なら読む。
    # 合計の候補(セグメント情報の「売上高」の行なども合うことがある)を順に試し、品目が当てはまって合計になる表を使う
    cands = []
    for ti, (n, v) in enumerate(rows):
        if _TOTAL.fullmatch(n) and len(v) > max(cur, prior or 0) and match(v[cur], unit):
            cands.append((ti, cur, prior, unit))
    # 今回の表と列の数・位置・単位が違う前の期の表(1Q は前年の列が無い、セグメントの列が少ない収益の分解の表など):
    # 合計の行(「外部顧客への売上高」なども)の中で連結売上と合う値の位置を当期の列とする(前年の列は読まない)
    for ti, (n, v) in enumerate(rows):
        if not (_TOTAL.fullmatch(n) or _TOTAL2.fullmatch(n)):
            continue
        for u in dict.fromkeys((unit, 1e6, 1e3)):
            for p in range(len(v)):
                if match(v[p], u) and not any(c[:2] == (ti, p) and c[3] == u for c in cands):
                    cands.append((ti, p, None, u))
    for ti, p, pr, u in cands:
        x = _pdf_table(rows, ti, {**tpl, "cur": p, "prior": pr, "unit": u}, strict=(pr is None and p != cur or u != unit))
        if x:
            return x
    return None


_TOTAL2 = re.compile(r"(\(\d\))?外部顧客(へ|に対する)の?(売上高|売上収益|営業収益)|顧客との契約から生じる収益")


_CELL = re.compile(r"[△▲\-]?[\d,]+(\.\d+)?%?|[-―－]")


def _restack(lines: list[str]) -> list[str]:
    """PDF の表で品目名と数値が列ごとに縦に並ぶ形(山崎製パン:「食パン」「菓子パン」…の後に「62,345」「243,606」…)を
    「品目名 値 値 …」の行に組み直す。品目名の行が m 行続いたあと、数値だけの行がちょうど m の倍数(列の数)続くときだけ。
    品目名の上に大分類の行(「食品事業」)があれば、m を減らして割り切れる数を探す"""
    out, i = [], 0
    while i < len(lines):
        j = i
        while j < len(lines) and not re.search(r"\d", lines[j]) and len(lines[j]) <= 30:
            j += 1
        k = j
        while k < len(lines) and _CELL.fullmatch(lines[k].replace(" ", "")):
            k += 1
        n, done = k - j, False
        for m in range(j - i, 1, -1):
            if n >= m * 2 and n % m == 0 and n // m <= 12:
                out.extend(lines[i:j - m])
                for r in range(m):
                    out.append(lines[j - m + r] + " " + " ".join(lines[j + c * m + r].replace(" ", "") for c in range(n // m)))
                i, done = k, True
                break
        if not done:
            out.append(lines[i])
            i += 1
    return out


def _pdf_table(rows: list, ti: int, tpl: dict, strict: bool = False) -> dict | None:
    """strict: 列の位置を合計の値から探したとき。品目の和が合計に合うことを必須にし(最大の小計との照合はしない)、
    「外部顧客への売上高」などの行があればその値も合計と合うこと(別の表の行を拾っていない)"""
    unit, cur, prior = tpl["unit"], tpl["cur"], tpl.get("prior")
    tot = rows[ti][1]
    norm = lambda n: re.sub(r"[ー・]", "", n)          # 「メモリー」と「メモリ」など表記の揺れ
    items, pos = [], max(0, ti - 3 * len(tpl["names"]) - 5)   # 合計の行のすぐ上の範囲だけ
    # すぐ上に別の表(受注高の表など)の合計の行があれば、その下から(1799: 受注高・売上高の表が同じ品目名で続く)
    prev = [j for j in range(pos, ti) if _TOTAL.fullmatch(rows[j][0]) and rows[j][0] not in ("計", "小計")]
    if prev:
        pos = prev[-1] + 1
    names = tpl["names"]                               # 品目名の並びどおりに、合計の行より上を順に当てる
    # 前年と当年の表が同じ品目名で続く短信(4224・5162: 前1Q の表のすぐ下に当1Q の表)は、合計の行にいちばん近い表から:
    # 最初の品目名が合計の行より上に最後に出てくる行から当て、品目の和が合計にちょうど合うときだけ使う。
    # 合わなければ(strict でなければ)今までどおり上から
    first = re.sub(r"[ー・]", "", names[0].split("・")[-1]) if names else ""
    last = [j for j in range(pos, ti) if first and re.sub(r"[ー・]", "", rows[j][0]).endswith(first)]
    if last and last[-1] != pos:
        x = _pdf_items(rows, ti, tpl, last[-1], True)
        if x or strict:
            return x
    return _pdf_items(rows, ti, tpl, pos, strict)


def _pdf_items(rows: list, ti: int, tpl: dict, pos: int, strict: bool) -> dict | None:
    unit, cur, prior = tpl["unit"], tpl["cur"], tpl.get("prior")
    tot, names, items = rows[ti][1], tpl["names"], []
    norm = lambda n: re.sub(r"[ー・]", "", n)
    start, used = pos, []

    def item(name, rv):
        # 名前の列(事業区分)が「―」の行(「不動産賃貸事業 ― 117,417 85.6」、4013): 合計の行より値が1つ多ければその「―」は除く
        if len(rv) == len(tot) + 1 and re.fullmatch(r"[-―－]", rv[0]):
            rv = rv[1:]
        v = _num(rv[cur]) if cur < len(rv) else None
        pv = _num(rv[prior]) if prior is not None and prior < len(rv) else None
        return {"name": name, "sales": None if v is None else v * unit,
                "sales_prior": None if pv is None else pv * unit, "sub": bool(re.search(r"小計|計$", name)),
                "adj": bool(_META.search(name))}
    for name in names:
        base = name.split("・")[-1]
        j = next((j for j in range(pos, ti) if len(rows[j][1]) > max(cur, prior or 0)
                  and (rows[j][0] == name or norm(rows[j][0]).endswith(norm(base))
                       or (re.search(r"小計$|計$", name) and rows[j][0] in ("小計", "計")))), None)
        if j is None:
            continue
        pos = j + 1
        used.append(j)
        items.append(item(name, rows[j][1]))
    tv = _num(tot[cur])
    if sum(not i["adj"] for i in items) < 2:
        return None
    if strict and any(re.search(r"外部顧客", i["name"]) and i["sales"] is not None and abs(i["sales"] - tv * unit) > unit * 2
                      for i in items):
        return None
    if not _check_sum([dict(i) for i in items], tv * unit, unit) and used:
        # 品目名が前の期と違う行(「物品販売(EC)」と今回の「物品販売(EC事業)」、9890)は名前では当たらない:
        # 当たった品目の間・合計の行の直前までの当たらなかった行を短信の名前のまま足して、ちょうど合計になるときだけ使う
        extra = [j for j in range(used[0], ti) if j not in used and len(rows[j][1]) > max(cur, prior or 0)
                 and not _TOTAL.fullmatch(rows[j][0]) and not _META.search(rows[j][0])]
        if 0 < len(extra) <= 2:
            trial = sorted([(j, it) for j, it in zip(used, items)] + [(j, item(rows[j][0], rows[j][1])) for j in extra],
                           key=lambda x: x[0])
            cand = [it for _, it in trial]
            if _check_sum([dict(i) for i in cand], tv * unit, unit):
                items = cand
    if not _check_sum(items, tv * unit, unit):
        # 折り返して読めなかった調整の行(「…セグメント間取引消去」など)があるときは、いちばん大きい小計(セグメント計)と照合
        subs = [i["sales"] for i in items if i["sub"] and i["sales"] is not None]
        if strict or not (subs and len(items) < len(names) and _check_sum(items, max(subs), unit)):
            return None
    tp = _num(tot[prior]) if prior is not None else None
    return {"items": items, "total": {"sales": tv * unit, "sales_prior": None if tp is None else tp * unit},
            "src": "irbank_pdf"}


def _cum_sales(hist: dict, code: str, ym: str, n_q: int) -> float | None:
    from .orders import _ym_shift
    qs = [hist.get(code, {}).get(_ym_shift(ym, -3 * k)) for k in range(n_q)]
    if any(q is None or q.get("sales") is None for q in qs):
        return None
    return sum(q["sales"] for q in qs)


def update(cfg: dict, data_dir: Path = DATA_DIR, limit: int = 300) -> int:
    """まだ読んでいない決算短信の内訳の表を読む(表が無ければ status=none)"""
    disc = disclosures.load(data_dir)
    have = set(load(data_dir)["disclosure_id"])
    arch = {a["disclosure_id"]: a for a in earnings.load_archive(data_dir).to_dict("records")}
    todo = disc[(disc["kind"] == "earnings_report") & (disc["is_correction"] != "True") & (disc["xbrl_url"] != "")
                & ~disc["disclosure_id"].isin(have) & disc["disclosure_id"].isin(arch)].sort_values("disclosed_at").head(limit)
    if todo.empty:
        return 0
    session = RateLimitedSession.from_config(cfg)
    segs = segment_names(data_dir)
    rows = []
    for r in todo.itertuples():
        a = arch[r.disclosure_id]
        cached = QUAL_DIR / f"{r.disclosure_id}.htm"
        x = None
        try:
            if cached.exists():                        # 手元に保存した補足情報(読み直し用)
                html = cached.read_bytes()
            else:
                content = tdnet.get_zip(session, r.xbrl_url)
                q = []
                if content and content[:2] == b"PK":            # TDnet は約1か月で消えるので、取れなければ none にして取り直さない
                    z = zipfile.ZipFile(io.BytesIO(content))
                    q = [n for n in z.namelist() if n.endswith("qualitative.htm")]
                html = z.read(q[0]) if q else b""
                if QUAL_DIR.parent.exists():
                    QUAL_DIR.mkdir(exist_ok=True)
                    cached.write_bytes(html)
        except Exception as e:
            log.warning("内訳: XBRL取得失敗 %s: %s", r.code, e)
            continue
        try:
            cs = pd.to_numeric(a.get("cum_sales"), errors="coerce")
            if html and not pd.isna(cs):
                x = parse_html(html.decode("utf-8", errors="replace"), float(cs), segs.get(r.disclosure_id, ()))
        except Exception as e:
            log.warning("内訳: 読み取り失敗 %s: %s", r.code, e)
        rows.append({"disclosure_id": r.disclosure_id, "code": r.code, "disclosed_at": r.disclosed_at,
                     "n_q": a.get("n_q", ""), "period_end": str(a.get("period_end", ""))[:7],
                     "status": "ok" if x else "none", "data": json.dumps(x, ensure_ascii=False) if x else ""})
    if rows:
        df = pd.concat([load(data_dir), pd.DataFrame(rows).reindex(columns=COLUMNS)])
        df.to_csv(path(data_dir), index=False, encoding="utf-8")
    log.info("breakdowns: %d (ok %d)", len(rows), sum(r["status"] == "ok" for r in rows))
    return len(rows)


def backfill_history(cfg: dict, depth: int = 7, data_dir: Path = DATA_DIR) -> dict:
    """内訳の表がある会社の過去の期を、手元の過去の短信 PDF から(今回の表の形・合計を連結売上と照合)。手元でのみ"""
    from .datasources import irbank
    from .orders import _irbank_earnings, _ym_shift, past_pdf_text
    session = irbank.session_from_config(cfg)
    df = load(data_dir)
    hist = {c: {q["end"]: q for q in v} for c, v in history.by_code(history.load(data_dir)).items()}
    have = {(r["code"], r["period_end"]) for r in df.to_dict("records") if r["status"] in ("ok", "bad")}
    latest = {}
    for r in df[df["status"] == "ok"].sort_values("disclosed_at").to_dict("records"):
        d = json.loads(r["data"])
        if d.get("tpl"):
            latest[r["code"]] = {**r, "data": d}
    rows, stats = [], {"codes": 0, "pdfs": 0, "ok": 0}
    for code, cur in latest.items():
        end = cur["period_end"]
        floor = _ym_shift(end, -3 * depth)
        try:
            items = [(i, q, ym) for i, q, ym in _irbank_earnings(session, code, fresh_after=str(cur["disclosed_at"])[:10])
                     if ym and floor <= ym < end and (code, ym) not in have]
        except Exception as e:
            log.warning("内訳の推移: 一覧の取得失敗 %s: %s", code, e)
            continue
        stats["codes"] += 1
        seen = set()
        for did, q, ym in items:
            if ym in seen:
                continue
            seen.add(ym)
            x = None
            try:
                text = past_pdf_text(session, code, did)
                stats["pdfs"] += 1
                if text:
                    x = parse_pdf_text(text, cur["data"]["tpl"], _cum_sales(hist, code, ym, int(q)))
            except Exception as e:
                log.warning("内訳の推移: PDF の読み取り失敗 %s %s: %s", code, did, e)
            if x:
                stats["ok"] += 1
            rows.append({"disclosure_id": did, "code": code, "disclosed_at": f"{did[4:8]}-{did[8:10]}-{did[10:12]}T00:00",
                         "n_q": str(q), "period_end": ym, "status": "ok" if x else "none",
                         "data": json.dumps(x, ensure_ascii=False) if x else ""})
    if rows:
        new = pd.DataFrame(rows).reindex(columns=COLUMNS)
        out = pd.concat([df[~df["disclosure_id"].isin(new["disclosure_id"])], new])
        out.sort_values(["disclosed_at", "disclosure_id"], ascending=False).to_csv(path(data_dir), index=False,
                                                                                 encoding="utf-8")
    log.info("breakdowns history: %s", stats)
    return stats
