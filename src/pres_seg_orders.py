"""決算説明資料の「前年 / 今年 / 増減」の形の、セグメント別の受注高・受注残高を読み、短信に合計しか無い期にセグメントを足す。

読み方は2通り(どちらも、合計の行が短信の受注合計(累計)と単位の丸めの範囲で一致し、セグメントを足すと合計になるときだけ):
  1) 表: 「受注高 1,978 3,596 +1,618 +81.8%」の合計の行の上か下に、同じ形のセグメントの行が並ぶ
  2) ページ: 事業ごとのページ(見出しに短信のセグメント名)に「受注高 786 2,313 +1,527」の行がある(栗田工業)
どの行も「増減 = 今年 − 前年」の検算が通ること(前年と今年の列だと確かめる。計画・予想の列は使わない)。
"""
import json
import logging
import re
import unicodedata
from pathlib import Path

import pandas as pd

from . import orders, pres_tables as pt
from .config import DATA_DIR

log = logging.getLogger(__name__)

UNITS = (1e6, 1e8, 1e3)
_ORD = re.compile(r"(連結|全社)?(受注高?|受注額|受注金額)(合計|計)?")
_BL = re.compile(r"(連結|全社)?(受注残高?|手持工事高|繰越工事高)(合計|計)?")
_TOT = re.compile(r"合計|総合計|計|連結|全社|全社計|連結合計")
_STOP = re.compile(r"利益|損失|原価|費|率|売上|収益|件数|人数|単価|台数|数量|EBITDA|ROE|ROIC")


def _ab(vals: list) -> tuple[float, float] | None:
    """「前年 今年 増減 …」の行の (前年, 今年)。増減(今年 − 前年)か増減率の検算が通らなければ None。
    前年と今年の間に前期末などの列が1つある表(能美防災「前年同期 | 前期 | 当期 | 前年同期比増減」)も"""
    if len(vals) < 3 or vals[0] is None:
        return None
    for j in (1, 2):
        if j >= len(vals) or vals[j] is None:
            continue
        a, b = vals[0], vals[j]
        if a == 0 and b == 0:
            continue
        tol = 1.01 if float(a).is_integer() and float(b).is_integer() else 0.11
        for d in vals[j + 1:j + 3]:
            if d is None:
                continue
            if abs((b - a) - d) <= tol:
                return a, b
            if j == 1 and a > 0 and abs((b / a - 1) * 100 - d) <= 0.06:
                return a, b                            # 「受注高 28,497 29,482 +3.5」(増減率だけ)
    return None


def _row(line: str):
    """「名前 数値 数値 … (後ろに説明の文があってもよい)」→ (名前, [数値])"""
    lab, vals = pt._row(line)
    if vals:
        return lab, vals
    toks = line.split()
    k = 0
    while k < len(toks) and not pt._NUMTOK.fullmatch(toks[k]):
        k += 1
    e = k
    while e < len(toks) and pt._NUMTOK.fullmatch(toks[e]):
        e += 1
    if e - k < 3:
        return lab, []
    return pt._UNIT_WORD.sub("", re.sub(r"\s", "", " ".join(toks[:k]))), [pt._num(t) for t in toks[k:e]]


def _lines(page: str) -> list[str]:
    ls = [re.sub(r"([+\-△▲])\s+(\d)", r"\1\2", unicodedata.normalize("NFKC", l)).strip() for l in page.splitlines()]
    return [l for l in ls if l]


def _unit(v: float, target: float) -> float | None:
    for u in UNITS:
        if abs(v * u - target) <= u * 1.01 + abs(target) * 1e-4:
            return u
    return None


def _close(x: float, y: float, n: int) -> bool:
    return abs(x - y) <= 0.5 * n + 1.01


def _key(n: str) -> str:
    return re.sub(r"\s|・|事業|セグメント|部門|分野|市場$", "", unicodedata.normalize("NFKC", n or ""))


def from_tables(pages: list[str], metric: re.Pattern, target: float) -> tuple[float, list[tuple[str, float, float]], tuple] | None:
    """1) 表: 合計の行(短信の値と一致)の上下に並ぶセグメントの行。
    受注高と受注残高が1行に並ぶ表(JFEシステムズ「合計 67,059 54,589 △18.6% 25,885 23,064 △10.9%」)は後ろの3列も見る。
    名前の無い行(ミライト・ワン: 合計・小計の行に名前が無い)は合計にしてよく、品目の間にあれば小計として飛ばす"""
    for page in pages:
        rows = [_row(l) for l in _lines(page)]
        for i, (lab, vals) in enumerate(rows):
            if not (metric.fullmatch(lab) or _TOT.fullmatch(lab) or lab == ""):
                continue
            for off in (0, 3):
                ab = _ab(vals[off:])
                u = ab and _unit(ab[1], target)
                if not u:
                    continue
                for step in (1, -1):
                    items, sa, sb = [], 0.0, 0.0
                    k = i + step
                    while 0 <= k < len(rows) and len(items) < 20:
                        l2, v2 = rows[k]
                        k += step
                        x = _ab(v2[off:])
                        if x and l2 == "" and items:
                            continue                   # 名前の無い小計の行
                        if not x or not l2 or _STOP.search(l2) or metric.fullmatch(l2) or _TOT.fullmatch(l2) or                                 _ORD.fullmatch(l2) or _BL.fullmatch(l2):
                            break
                        items.append((pt._clean(l2), x[0], x[1]))
                        sa, sb = sa + x[0], sb + x[1]
                        if len(items) >= 2 and _close(sa, ab[0], len(items)) and _close(sb, ab[1], len(items)):
                            names = [n for n, _, _ in items]
                            # 「小計」の行(コムシス: 通信キャリア事業の小計)はセグメントの名前にならないので採らない
                            if all(len(n) >= 2 and not re.fullmatch(r"小計|計|その他計", n) for n in names) and                                     len(set(names)) == len(names):
                                return u, (items if step == 1 else items[::-1]), ab
                            break
    return None


_NOT_NAME = re.compile(r"\d|期|年|月|予想|実績|計画|見通し|増減|単位|前年|当期|比|差|%|率|合計|連結|全社|受注|売上|利益|資料|決算|概要|サマリ|業績|推移|ページ|Copyright|©")


def _name_before(ls: list[str], i: int) -> str | None:
    """行のすぐ上(2行まで)の、数値の無い名前だけの行(「塗工機関連機器」→ 受注高の行)"""
    for k in (i - 1, i - 2):
        if k < 0:
            return None
        l = ls[k]
        if re.search(r"\d", l):
            return None
        n = pt._clean(re.sub(r"\s", "", l))
        if 2 <= len(n) <= 25 and not _NOT_NAME.search(n):
            return n
    return None


_SALES = re.compile(r"売上高|売上収益|営業収益|売上高\(.*\)|外部顧客への売上高")


def _by_sales(ls: list[str], i: int, seg_sales: dict[str, float]) -> str | None:
    """受注高の行のすぐ下(6行まで)の売上高の行の今年の値が、短信のセグメントの売上と一致するセグメント(能美防災)"""
    for l in ls[i + 1:i + 7]:
        lab, vals = _row(l)
        ab = _SALES.fullmatch(lab) and _ab(vals)
        if not ab:
            continue
        hit = [n for n, v in seg_sales.items() if v and _unit(ab[1], v)]
        return hit[0] if len(hit) == 1 else None
    return None


def from_pages(pages: list[str], metric: re.Pattern, target: float, seg_names: list[str],
               seg_sales: dict[str, float] | None = None):
    """2) 事業ごとの受注高の行: 名前は、すぐ上の事業名の行(ヒラノテクシード)か、そのページにある短信のセグメント名(栗田工業)。
    合計の行(短信と一致)より後の行を順に足して合計になる並び。名前が分からない行は1つまで(残りのセグメントが1つのときだけ)"""
    keys = {_key(n): n for n in seg_names if len(_key(n)) >= 2}
    cand = []                                          # (名前 or None, 前年, 今年)
    for page in pages:
        ls = _lines(page)
        on_page = {n for k, n in keys.items() if k in re.sub(r"\s", "", page)}
        for i, l in enumerate(ls):
            lab, vals = _row(l)
            ab = metric.fullmatch(lab) and _ab(vals)
            if not ab:
                continue
            name = _name_before(ls, i) or _by_sales(ls, i, seg_sales or {}) or                 (next(iter(on_page)) if len(on_page) == 1 else None)
            cand.append((name, ab[0], ab[1]))
    t = next((j for j, c in enumerate(cand) if _unit(c[2], target)), None)
    if t is None:
        return None
    total = cand[t][1:]
    u = _unit(total[1], target)
    for j in range(t + 1, len(cand)):
        sa = sb = 0.0
        for k in range(j, min(len(cand), j + 15)):
            sa, sb = sa + cand[k][1], sb + cand[k][2]
            n = k - j + 1
            if n >= 2 and _close(sa, total[0], n) and _close(sb, total[1], n):
                run = cand[j:k + 1]
                named = [c[0] for c in run if c[0]]
                if len(set(named)) != len(named) or len(named) < n - 1:
                    break
                if len(named) == n - 1:                # 名前の無い行は、短信のセグメントで残った1つ
                    rest = [x for x in seg_names if x not in named and _key(x) not in {_key(m) for m in named}]
                    if len(rest) != 1 or len(seg_names) != n:
                        break
                    run = [(c[0] or rest[0], c[1], c[2]) for c in run]
                return u, [(c[0], c[1], c[2]) for c in run], total
            if abs(sb) > abs(total[1]) * 1.5 + n:
                break
    return None


def from_heading(pages: list[str], metric: re.Pattern, target: float, prior: float | None):
    """3) 「受注高」だけの見出しの行の下に「事業名 前年 今年 増減」の行が並び、足すと短信の合計になる(三機工業)"""
    for page in pages:
        ls = _lines(page)
        for i, l in enumerate(ls):
            if not metric.fullmatch(re.sub(r"\s|[(（].*[)）]$", "", l)):
                continue
            items = []
            for l2 in ls[i + 1:i + 22]:
                lab, vals = _row(l2)
                x = _ab(vals)
                if not x or not lab or _STOP.search(lab) or _ORD.fullmatch(lab) or _BL.fullmatch(lab):
                    break
                items.append((pt._clean(lab), x[0], x[1]))
            for n in range(len(items), 1, -1):          # 合計の行が最後にあれば除いて足す
                run = items[:n]
                if _TOT.fullmatch(run[-1][0]):
                    continue
                sb = sum(x[2] for x in run)
                u = _unit(sb, target)
                if not u or abs(sb * u - target) > u * (0.5 * n + 1.01):
                    continue
                sa = sum(x[1] for x in run)
                if prior is not None and abs(sa * u - prior) > u * (0.5 * n + 1.01):
                    continue
                names = [x[0] for x in run]
                if len(set(names)) == n and all(len(x) >= 2 and not re.fullmatch(r"小計|計", x) for x in names):
                    return u, run, (sa, sb)
    return None


def read(text: str, rec: dict, seg_names: list[str], seg_sales: dict[str, float] | None = None) -> dict | None:
    """短信の受注の記録(合計のみ)に足すセグメント: {名前: {orders, orders_prior, backlog, backlog_*}}"""
    pages = text.split("\f") if "\f" in text else [text]
    t = rec["total"]
    out: dict[str, dict] = {}
    for key, metric in (("orders", _ORD), ("backlog", _BL)):
        if not t.get(key):
            continue
        got = from_tables(pages, metric, t[key]) or from_pages(pages, metric, t[key], seg_names, seg_sales) or             from_heading(pages, metric, t[key], t.get("orders_prior") if key == "orders" else None)
        if not got:
            continue
        u, items, (ta, tb) = got
        for n, a, b in items:
            s = out.setdefault(n, {"name": n})
            s[key] = b * u
            if key == "orders":
                if t.get("orders_prior") is None or abs(ta * u - t["orders_prior"]) <= u * 1.01 + abs(t["orders_prior"]) * 1e-4:
                    s["orders_prior"] = a * u
            else:                                      # 前年同期末か前期末か、合計の列で決める
                for f in ("backlog_prior", "backlog_fy"):
                    if t.get(f) is not None and abs(ta * u - t[f]) <= u * 1.01 + abs(t[f]) * 1e-4:
                        s[f] = a * u
                        break
    return out or None


def _segments(found: dict) -> list[dict]:
    segs = []
    for s in found.values():
        e = {"name": s["name"]}
        for m in ("orders", "backlog"):
            for suf in ("", "_prior", "_fy"):
                e[m + suf] = s.get(m + suf)
            p = s.get(f"{m}_prior")
            e[f"{m}_yoy"] = None if s.get(m) is None or not p else round((s[m] / p - 1) * 100, 1)
        segs.append(e)
    return segs


def _seg_names(code: str, data_dir: Path) -> list[str]:
    """短信のセグメント名(最新の期の並び。調整・全社は除く)"""
    from . import segments
    sdf = segments.load(data_dir)
    rs = sdf[(sdf["code"] == code) & (sdf["status"] == "ok")].sort_values("period_end")
    for r in rs.to_dict("records")[::-1]:
        try:
            n = [x["name"] for x in json.loads(r["data"]).get("segments", []) if not re.search(r"調整|消去|全社", x["name"])]
        except (ValueError, TypeError):
            continue
        if n:
            return n
    return []


def fill(texts_for, data_dir: Path = DATA_DIR, codes: set[str] | None = None, only_ids: set[str] | None = None) -> dict:
    """セグメントの無い受注の記録に、同じ日(〜3日後)の説明資料のセグメントを足す。
    texts_for(code, date) → その会社のその日〜3日後の説明資料の [(番号, 文字)]"""
    df = orders.load(data_dir)
    rows = []
    names_cache: dict[str, list[str]] = {}
    from . import segments
    seg_sales: dict[tuple, dict[str, float]] = {}          # (code, 年月) → {セグメント: 累計売上}
    sdf = segments.load(data_dir)
    want = set(df[df["status"] == "ok"]["code"]) if codes is None else set(codes)
    if only_ids is not None:
        want = set(df[df["disclosure_id"].isin(only_ids)]["code"])
    for r in sdf[(sdf["status"] == "ok") & sdf["code"].isin(want)].to_dict("records"):
        try:
            seg_sales[(r["code"], str(r["period_end"])[:7])] = {
                x["name"]: x.get("sales") for x in json.loads(r["data"]).get("segments", [])}
        except (ValueError, TypeError):
            pass
    for r in df[df["status"] == "ok"].to_dict("records"):
        if codes is not None and r["code"] not in codes or only_ids is not None and r["disclosure_id"] not in only_ids:
            continue
        d = json.loads(r["data"])
        if d.get("segments") and d.get("seg_src") != "pres_yoy":
            continue
        tot = d.get("total") or {}
        if not tot.get("orders") and not tot.get("backlog"):
            continue
        names = names_cache.setdefault(r["code"], _seg_names(r["code"], data_dir))
        sales = seg_sales.get((r["code"], str(r["period_end"])[:7]), {})
        for doc, text in texts_for(r["code"], r["disclosed_at"][:10]):
            found = read(text, d, names, sales)
            if found and len(found) >= 2:
                segs = _segments(found)
                if d.get("segments") == segs:
                    break
                d["segments"], d["seg_src"], d["seg_doc"] = segs, "pres_yoy", doc
                rows.append({**r, "data": json.dumps(d, ensure_ascii=False)})
                break
    if rows:
        out = pd.concat([df[~df["disclosure_id"].isin([x["disclosure_id"] for x in rows])],
                         pd.DataFrame(rows).reindex(columns=orders.COLUMNS)])
        out.sort_values(["disclosed_at", "disclosure_id"], ascending=False).to_csv(orders.path(data_dir), index=False,
                                                                                 encoding="utf-8")
    return {"filled": len(rows), "codes": sorted({x["code"] for x in rows})}


def _texts_local(data_dir: Path):
    """手元(~/kessan-pdfs の説明資料・pres_text の取得済み)から"""
    from . import disclosures
    from .datasources import irbank_api, tdnet
    idx = irbank_api.load_index()
    idx = idx[(idx["doc_id"] != "") & (idx["source"] == "tdnet")]
    kind = idx["title"].map(lambda t: tdnet.classify_title(t)["kind"])
    by: dict[str, list[tuple[str, str]]] = {}
    for p in idx[kind == "earnings_presentation"].to_dict("records"):
        by.setdefault(p["code"], []).append((p["filing_date"], p["doc_id"]))
    disc = disclosures.load(data_dir)
    for p in disc[disc["kind"] == "earnings_presentation"].to_dict("records"):
        by.setdefault(p["code"], []).append((p["disclosed_at"][:10], p["disclosure_id"][4:]))

    def texts_for(code, date):
        hi = (pd.Timestamp(date) + pd.Timedelta(days=3)).strftime("%Y-%m-%d")
        out = []
        for dt, doc in sorted(set(by.get(code, []))):
            if not date <= dt <= hi:
                continue
            f = pt.PDF_DIR / code / f"{doc}.pdf"
            c = pt.PDF_DIR / "pres_text" / f"{doc}.txt"
            if c.exists():
                t = c.read_text(encoding="utf-8")
            elif f.exists():
                t = pt._pdf_text(f.read_bytes())
                try:
                    c.parent.mkdir(parents=True, exist_ok=True)
                    c.write_text(t, encoding="utf-8")
                except OSError:
                    pass
            else:
                t = ""
            if t:
                out.append((doc, t))
        return out
    return texts_for


def build_local(data_dir: Path = DATA_DIR, codes: set[str] | None = None) -> dict:
    res = fill(_texts_local(data_dir), data_dir, codes)
    log.info("説明資料のセグメント別受注: %s", res)
    return res


def update(cfg: dict, data_dir: Path = DATA_DIR) -> int:
    """監視の1回: 直近3日の短信の受注(合計のみ)に、取得済みの説明資料の文字(pres_tables が保存)からセグメントを足す"""
    df = orders.load(data_dir)
    since = (pd.Timestamp.now(tz="Asia/Tokyo") - pd.Timedelta(days=3)).strftime("%Y-%m-%d")
    ids = set(df[(df["status"] == "ok") & (df["disclosed_at"].str[:10] >= since)]["disclosure_id"])
    if not ids:
        return 0
    return fill(_texts_local(data_dir), data_dir, only_ids=ids)["filled"]
