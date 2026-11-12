"""決算説明資料・データシートの「四半期ごとの推移」の表から、品目別(商品別・製品別など)の売上高と受注高・受注残高を読む。

例(栄電子 7567「商品別 売上高・受注高推移」):
    2026年3月期 2027年3月期 前年同期比
    ２Q 3Q 4Q 1Q ２Q (増減額) (増減率%)
    売上高 1,612 1,838 2,124 2,781 3,005 +1,392 +86.4
    一般電子部品 990 1,142 1,296 1,746 1,849 ...
    受注高 1,716 2,184 2,993 3,515 4,745 ...
    一般電子部品 1,072 1,368 ...

読むのは、次をすべて満たす表だけ(数値を間違えて出さないため):
  - 見出しの行に四半期(1Q〜4Q)が3つ以上、順番どおりに並ぶ(年度の列 2026/03 などが間に入ってもよい)
  - 表の売上高(合計)の行が、短信の四半期ごとの連結売上(3か月)と、表の単位の丸め(±1)の範囲で
    すべての四半期で一致する(2か所以上・1Q 以外を含む。累計の表や別の会社・別の期間の表はここで落ちる)
  - 品目の行を足すと、その合計の行になる(すべての四半期で、品目数ぶんの丸めの範囲)
受注高・受注残高の品目は、売上高で確かめた同じ表の中にあるときだけ読む。
四半期ごとの3か月の値を、その期の決算短信の記録(累計)にして data/breakdowns.csv・data/orders.csv に入れる
(短信の表から読めている期は上書きしない。src="presentation")。
"""
import json
import logging
import re
import unicodedata
from pathlib import Path

import pandas as pd

from . import breakdown, disclosures, history, orders
from .config import DATA_DIR
from .datasources import tdnet

log = logging.getLogger(__name__)

PDF_DIR = Path.home() / "kessan-pdfs"
UNITS = (1e6, 1e3, 1e8)
_Q = re.compile(r"(?:Q([1-4])|([1-4])Q)")
_YEAR = re.compile(r"\d{4}[/.]\d{1,2}|\d{4}年\d{1,2}月期?|FY\d{2,4}|通期|\d{2}/\d{1,2}期")
_NUMTOK = re.compile(r"[+\-△▲]?\d[\d,]*(?:\.\d+)?%?|\(\d[\d,]*(?:\.\d+)?\)|[-―—–]")
_UNIT_WORD = re.compile(r"\s*(百万円|千円|億円|円|人|株|%|倍)\s*$")
SALES = re.compile(r"(連結)?(売上高|売上収益|営業収益|売上)(合計|計)?|合計|総合計|総計")
ORDERS = re.compile(r"(連結)?(受注高|受注額|受注)(合計|計)?")
BACKLOG = re.compile(r"(連結)?(受注残高?|受注残)(合計|計)?")
_STOP = re.compile(r"利益|損失|原価|費|率|比|増減|構成|前年|前期|EBITDA|件数|人数|単価|台数|数量")


def _num(t: str) -> float | None:
    if re.fullmatch(r"[-―—–]", t):
        return None
    neg = t.startswith("(") or t[0] in "△▲-"
    v = float(re.sub(r"[^\d.]", "", t))
    return -v if neg else v


def _row(line: str):
    """「ラベル 数値 数値 …」→ (ラベル, [数値 or None])。数値が無い行は (ラベル, [])"""
    toks = line.split()
    k = len(toks)
    while k > 0 and _NUMTOK.fullmatch(toks[k - 1]):
        k -= 1
    label = re.sub(r"\s", "", " ".join(toks[:k]))
    label = _UNIT_WORD.sub("", label)
    return label, [_num(t) for t in toks[k:]]


def _header(line: str):
    """四半期の見出しの行なら、列の候補(年度の列を列に数える/数えない)を返す: [[("Q", n) | ("Y", None)], ...]"""
    toks = line.split()
    cols = []
    for t in toks:
        m = _Q.fullmatch(t)
        if m:
            cols.append(("Q", int(m.group(1) or m.group(2))))
        elif _YEAR.fullmatch(t):
            cols.append(("Y", None))
    qs = [c for c in cols if c[0] == "Q"]
    if len(qs) < 3:
        return None
    for a, b in zip(qs, qs[1:]):
        if b[1] != a[1] % 4 + 1:
            return None
    out = [qs]
    if len(cols) != len(qs):
        out.append(cols)
    return out


def _ym_shift(ym: str, months: int) -> str:
    return orders._ym_shift(ym, months)


def _place(cols, vals, cur_col: int, ym: str) -> dict[int, str]:
    """列 → 四半期末の年月(cur_col が ym)"""
    qcols = [j for j, c in enumerate(cols) if c[0] == "Q"]
    pos = qcols.index(cur_col)
    return {j: _ym_shift(ym, 3 * (i - pos)) for i, j in enumerate(qcols)}


def _close(v: float, h: float, unit: float) -> bool:
    return abs(v * unit - h) <= unit * 1.01 + abs(h) * 0.0005


def _anchor(cols, vals, n_q: int, ym: str, hq: dict):
    """売上高の行を短信の3か月の連結売上と照合: 合えば (列→年月, 単位)"""
    cand = [j for j, c in enumerate(cols) if c[0] == "Q" and c[1] == n_q and j < len(vals) and vals[j] is not None]
    for j in cand:
        ymap = _place(cols, vals, j, ym)
        for unit in UNITS:
            hit, bad, non1 = 0, False, False
            for c, y in ymap.items():
                if c >= len(vals) or vals[c] is None or hq.get(y) is None:
                    continue
                if _close(vals[c], hq[y], unit):
                    hit += 1
                    non1 = non1 or cols[c][1] != 1
                else:
                    bad = True
                    break
            if not bad and hit >= 2 and non1:
                return ymap, unit
    return None


def _items_after(rows, i, ymap, unit, step=1):
    """rows[i] を合計として、下(step=1)または上(step=-1)に続く品目の行を、足すと合計になるところまで取る"""
    tot = rows[i][1]
    cols = [c for c in ymap if c < len(tot) and tot[c] is not None]
    if not cols:
        return None
    acc, items = {c: 0.0 for c in cols}, []
    k = i + step
    while 0 <= k < len(rows) and len(items) < 25:
        lab, vals = rows[k]
        k += step
        if not vals:
            continue
        if not lab or _STOP.search(lab) or SALES.fullmatch(lab) or ORDERS.fullmatch(lab) or BACKLOG.fullmatch(lab):
            return None
        items.append((lab, vals))
        for c in cols:
            acc[c] += (vals[c] or 0) if c < len(vals) else 0
        n = len(items)
        if n >= 2 and all(abs(acc[c] - tot[c]) <= 0.5 * n + 1.01 for c in cols):
            return items if step == 1 else items[::-1]
    return None


def _series(vals, ymap, unit, upto: str) -> dict[str, float]:
    """列の値(円)。今回の期より後の列(予想)は使わない"""
    return {y: vals[c] * unit for c, y in ymap.items() if c < len(vals) and vals[c] is not None and y <= upto}


_MARK = re.compile(r"^[└├┗┣‐\-–—‘'’・※]+|^うち[、,]?|※\d*$")


def _clean(name: str) -> str:
    n = _MARK.sub("", name)
    m = re.fullmatch(r"\((.+)\)", n)
    return m.group(1) if m else n


def parse_text(text: str, n_q: int, ym: str, hq: dict) -> list[dict]:
    """説明資料の文字から、確かめの通った表: [{"title", "unit", "sales"|"orders"|"backlog": {"total": {年月: 円},
    "items": [(名前, {年月: 円})]}, "q": {年月: 四半期}}]。hq は {年月: 3か月の連結売上(円)}"""
    out = []
    pages = text.split("\f") if "\f" in text else [text]
    for page in pages:
        lines = [re.sub(r"([+\-△▲])\s+(\d)", r"\1\2", unicodedata.normalize("NFKC", l)).strip()
                 for l in page.splitlines()]
        lines = [l for l in lines if l]
        title = next((re.sub(r"\s+", " ", l) for l in lines[:6] if re.search(breakdown.KINDS, l)), None)
        heads = [(i, h) for i, l in enumerate(lines) if (h := _header(l))]
        for a, (hi, cands) in enumerate(heads):
            end = heads[a + 1][0] if a + 1 < len(heads) else len(lines)
            rows = [_row(l) for l in lines[hi + 1:end]]
            got = None
            for cols in cands:
                for r, (lab, vals) in enumerate(rows):
                    if vals and SALES.fullmatch(lab):
                        an = _anchor(cols, vals, n_q, ym, hq)
                        if an:
                            got = (cols, r, an)
                            break
                if got:
                    break
            if not got:
                continue
            cols, r0, (ymap, unit) = got
            tbl = {"title": title, "unit": unit, "q": {y: cols[c][1] for c, y in ymap.items()}}
            for r, (lab, vals) in enumerate(rows):
                kind = ("sales" if r == r0 or (SALES.fullmatch(lab) and r > r0) else
                        "orders" if ORDERS.fullmatch(lab) else "backlog" if BACKLOG.fullmatch(lab) else None)
                if not vals or kind is None or kind in tbl:
                    continue
                if kind == "sales" and r != r0:
                    an = _anchor(cols, vals, n_q, ym, hq)
                    if not an or an[0] != ymap:
                        continue
                items = _items_after(rows, r, ymap, unit) or \
                    (_items_after(rows, r, ymap, unit, -1) if re.search(r"合計|計$", lab) else None)
                names = [_clean(n) for n, _ in items or []]
                # 行の名前が折り返しで切れた表(「ホテ / ル」)・同じ名前が2つある表は品目を読まない
                if any(len(n) < 2 for n in names) or len(set(names)) != len(names):
                    items = []
                tbl[kind] = {"total": _series(vals, ymap, unit, ym),
                             "items": [(_clean(n), _series(v, ymap, unit, ym)) for n, v in items or []]}
            if any(tbl.get(k, {}).get("items") for k in ("sales", "orders", "backlog")) or "orders" in tbl:
                out.append(tbl)
    return out


# ---- 会社ごとに、説明資料の表(新しいものを優先)から各期の累計の記録を作る ----

def merge_tables(tables: list[tuple[str, dict]]) -> dict:
    """[(説明資料の日付, 表)] → {kind: {"total": {年月: 円}, "items": {名前: {年月: 円}}, "names": [..]}, "q": {年月: 四半期},
    "title", "unit"}。同じ四半期は新しい資料の値"""
    out = {"q": {}, "title": None, "unit": None}
    for _, t in sorted(tables, key=lambda x: x[0]):
        out["q"].update(t["q"])
        out["title"] = t.get("title") or out["title"]
        out["unit"] = t["unit"]
        for kind in ("sales", "orders", "backlog"):
            if kind not in t:
                continue
            m = out.setdefault(kind, {"total": {}, "items": {}, "names": []})
            m["total"].update(t[kind]["total"])
            if t[kind]["items"]:
                m["names"] = [n for n, _ in t[kind]["items"]]
            for n, s in t[kind]["items"]:
                m["items"].setdefault(n, {}).update(s)
    return out


def _cum(s: dict, ym: str, n_q: int) -> float | None:
    vs = [s.get(_ym_shift(ym, -3 * j)) for j in range(n_q)]
    return None if any(v is None for v in vs) else sum(vs)


def _pct(c, p):
    return None if c is None or not p else round((c / p - 1) * 100, 1)


_ADJ = re.compile(r"調整|消去|全社|セグメント間")


def breakdown_data(m: dict, ym: str, n_q: int, doc: str) -> dict | None:
    s = m.get("sales")
    if not s or len(s["names"]) < 2 or m["q"].get(ym) != n_q:
        return None
    tot = _cum(s["total"], ym, n_q)
    if tot is None:
        return None
    items = []
    for n in s["names"]:
        v = _cum(s["items"].get(n, {}), ym, n_q)
        if v is None:
            return None
        items.append({"name": n, "sales": v, "sales_prior": _cum(s["items"][n], _ym_shift(ym, -12), n_q),
                      "sub": False, "adj": bool(_ADJ.search(n))})
    return {"title": m.get("title") or "売上の内訳(説明資料)", "unit": m["unit"], "items": items,
            "total": {"sales": tot, "sales_prior": _cum(s["total"], _ym_shift(ym, -12), n_q)},
            "src": "presentation", "pres_doc": doc}


def orders_data(m: dict, ym: str, n_q: int, doc: str) -> dict | None:
    o, b = m.get("orders"), m.get("backlog")
    if m["q"].get(ym) != n_q or not (o or b):
        return None
    py = _ym_shift(ym, -12)

    def entry(name, os, bs):
        c = _cum(os, ym, n_q) if os is not None else None
        p = _cum(os, py, n_q) if os is not None else None
        bl, bp = (bs or {}).get(ym), (bs or {}).get(py)
        return {"name": name, "orders": c, "orders_prior": p, "orders_fy": None, "backlog": bl, "backlog_prior": bp,
                "backlog_fy": (bs or {}).get(_ym_shift(ym, -3 * n_q)), "orders_yoy": _pct(c, p), "backlog_yoy": _pct(bl, bp)}

    total = entry("合計", o["total"] if o else None, b["total"] if b else None)
    if total["orders"] is None and total["backlog"] is None:
        return None
    names = list(dict.fromkeys((o["names"] if o else []) + (b["names"] if b else [])))
    segs = [entry(n, (o or {}).get("items", {}).get(n) if o and n in o["names"] else None,
                  (b or {}).get("items", {}).get(n) if b and n in b["names"] else None) for n in names]
    segs = [x for x in segs if x["orders"] is not None or x["backlog"] is not None]
    if len(segs) < 2:
        segs = []
    return {"total": total, "segments": segs, "src": "presentation", "pres_doc": doc}


# ---- 書き込み ----

def _key(n: str) -> str:
    return re.sub(r"\s|・", "", unicodedata.normalize("NFKC", n or ""))


def _write(df: pd.DataFrame, rows: list[dict], cols: list[str], p: Path) -> None:
    out = pd.concat([df[~df["disclosure_id"].isin([r["disclosure_id"] for r in rows])],
                     pd.DataFrame(rows).reindex(columns=cols)])
    out.sort_values(["disclosed_at", "disclosure_id"], ascending=False).to_csv(p, index=False, encoding="utf-8")


def apply(merged: dict[str, dict], reports: dict[str, dict[str, dict]], data_dir: Path = DATA_DIR,
          only_ids: set[str] | None = None) -> dict:
    """merged: code → merge_tables の結果(+ "docs": {年月: 説明資料の番号})。reports: code → {年月: 短信 {disclosure_id, disclosed_at, n_q}}。
    短信の表から読めている期は上書きしない。受注は、短信に合計しか無く説明資料の品目の合計が短信と合う期は品目を足す"""
    bdf, odf = breakdown.load(data_dir), orders.load(data_dir)
    b_by = {r["disclosure_id"]: r for r in bdf.to_dict("records")}
    o_by = {r["disclosure_id"]: r for r in odf.to_dict("records")}
    b_rows, o_rows = [], []
    from . import segments
    sdf = segments.load(data_dir)
    seg_names: dict[str, set[str]] = {}
    for r in sdf[sdf["status"] == "ok"].to_dict("records"):
        try:
            seg_names.setdefault(r["code"], set()).update(
                _key(re.sub(r"(事業|セグメント|部門)$", "", x["name"])) for x in json.loads(r["data"]).get("segments", []))
        except (ValueError, TypeError):
            pass
    seg_names = {c: v | {x + "事業" for x in v} for c, v in seg_names.items()}
    for code, m in merged.items():
        _rename_to_tanshin(m, code, reports.get(code, {}), odf, sdf)
        seg_names.setdefault(code, set()).update(_key(n) for n in _short_names(sdf, code))
        # 短信の内訳の表がある会社は、品目の名前が同じときだけ(違う分け方を混ぜない)
        own = [json.loads(r["data"]) for r in bdf[(bdf["code"] == code) & (bdf["status"] == "ok")].to_dict("records")]
        own = [d for d in own if d.get("src") != "presentation"]
        names = {_key(n) for n in (m.get("sales") or {}).get("names", []) if not _ADJ.search(n)}
        names_ok = not own or {_key(i["name"]) for i in own[0]["items"]} == names
        # 短信のセグメント情報と同じ分け方(セグメント別の売上の推移)は、セグメントの表に出ているので内訳にしない
        if names and names <= seg_names.get(code, set()):
            names_ok = False
        for ym, rep in reports.get(code, {}).items():
            did = rep["disclosure_id"]
            doc = m.get("docs", {}).get(ym, "")
            base = {"disclosure_id": did, "code": code, "disclosed_at": rep["disclosed_at"], "n_q": str(rep["n_q"]),
                    "period_end": ym, "status": "ok"}

            def free(old):
                """書いてよいか: 記録が無い・読めなかった期、または説明資料から作った記録(全社の作り直しか今回の短信のとき)"""
                if not old or old["status"] != "ok":
                    return True
                if '"src": "presentation"' not in old["data"]:
                    return False
                return only_ids is None or did in only_ids

            old = b_by.get(did)
            if names_ok and free(old):
                d = breakdown_data(m, ym, rep["n_q"], doc)
                if d and (not old or old["data"] != json.dumps(d, ensure_ascii=False)):
                    b_rows.append({**base, "data": json.dumps(d, ensure_ascii=False)})
            old = o_by.get(did)
            d = orders_data(m, ym, rep["n_q"], doc)
            if not d:
                continue
            if old and old["status"] == "ok" and '"src": "presentation"' not in old["data"]:
                od = json.loads(old["data"])
                t = od.get("total") or {}
                # 短信に合計しか無い: 品目の累計の合計が短信の合計と合うときだけ品目を足す
                if od.get("segments") and od.get("seg_src") != "presentation":
                    continue
                if not d["segments"] or t.get("orders") is None or d["total"]["orders"] is None:
                    continue
                if abs(t["orders"] - d["total"]["orders"]) > m["unit"] * (rep["n_q"] + 1):
                    continue
                if od.get("segments") == d["segments"]:
                    continue
                od["segments"] = d["segments"]
                od["seg_src"], od["pres_doc"] = "presentation", doc
                o_rows.append({**base, "n_q": old["n_q"], "period_end": old["period_end"],
                               "data": json.dumps(od, ensure_ascii=False)})
                continue
            if not free(old):
                continue
            if old and old["status"] == "ok" and not d["segments"] and json.loads(old["data"]).get("segments"):
                continue
            row = {**base, "period_end": f"{ym}-{_month_end(ym)}", "data": json.dumps(d, ensure_ascii=False)}
            if not old or old["data"] != row["data"]:
                o_rows.append(row)
    if b_rows:
        _write(bdf, b_rows, breakdown.COLUMNS, breakdown.path(data_dir))
    if o_rows:
        _write(odf, o_rows, orders.COLUMNS, orders.path(data_dir))
    return {"breakdowns": len(b_rows), "orders": len(o_rows),
            "codes": sorted({r["code"] for r in b_rows + o_rows})}


def _short_names(sdf: pd.DataFrame, code: str) -> list[str]:
    out = []
    for r in sdf[(sdf["code"] == code) & (sdf["status"] == "ok")].to_dict("records"):
        try:
            out += [x["name"] for x in json.loads(r["data"]).get("segments", [])]
        except (ValueError, TypeError):
            pass
    return out


def _rename_to_tanshin(m: dict, code: str, reps: dict, odf: pd.DataFrame, sdf: pd.DataFrame) -> None:
    """説明資料の略称(FUJI の「RS」「MT」)を短信のセグメント名(「ロボットソリューション」)にそろえる。
    同じ期の累計の金額が短信の受注(またはセグメントの売上)と単位の丸めの範囲で一致し、相手が1つに決まる名前だけ"""
    tan: dict[str, dict[str, float]] = {}                # 短信の名前 → {年月: 累計}
    for kind, df, f in (("orders", odf, "orders"), ("sales", sdf, "sales")):
        for r in df[(df["code"] == code) & (df["status"] == "ok")].to_dict("records"):
            try:
                d = json.loads(r["data"])
            except (ValueError, TypeError):
                continue
            if d.get("src") == "presentation" or d.get("seg_src") == "presentation":
                continue
            ym = str(r["period_end"])[:7]
            for s in d.get("segments", []):
                if s.get(f) is not None:
                    tan.setdefault(kind, {}).setdefault(s["name"], {})[ym] = (s[f], int(float(r["n_q"])))
    alias = {}
    for kind in ("orders", "sales"):
        x = m.get(kind)
        if not x or not tan.get(kind):
            continue
        for n in x["names"]:
            if _key(n) in {_key(t) for t in tan[kind]}:
                continue
            hits = set()
            for t, vals in tan[kind].items():
                ok = [abs(v - c) <= m["unit"] * (nq + 1) for ym, (v, nq) in vals.items()
                      if (c := _cum(x["items"].get(n, {}), ym, nq)) is not None]
                if ok and all(ok):
                    hits.add(t)
            if len(hits) == 1:
                alias.setdefault(n, set()).update(hits)
    alias = {n: next(iter(t)) for n, t in alias.items() if len(t) == 1}
    if not alias:
        return
    for kind in ("sales", "orders", "backlog"):
        x = m.get(kind)
        if x:
            x["names"] = [alias.get(n, n) for n in x["names"]]
            x["items"] = {alias.get(n, n): v for n, v in x["items"].items()}


def _month_end(ym: str) -> str:
    return str((pd.Timestamp(f"{ym}-01") + pd.offsets.MonthEnd(0)).day)


def _hq(data_dir: Path) -> dict[str, dict[str, float]]:
    return {c: {q["end"]: q["sales"] for q in v if q.get("sales") is not None}
            for c, v in history.by_code(history.load(data_dir)).items()}


def _reports_from_disc(disc: pd.DataFrame) -> dict[str, dict[str, dict]]:
    out: dict[str, dict[str, dict]] = {}
    reps = disc[(disc["kind"] == "earnings_report") & (disc["is_correction"] != "True")]
    for r in reps.sort_values("disclosed_at").to_dict("records"):
        ym = orders.period_end_of_title(r["title"])
        if ym:
            out.setdefault(r["code"], {})[ym] = {"disclosure_id": r["disclosure_id"], "disclosed_at": r["disclosed_at"],
                                                 "n_q": orders._quarter_of_title(r["title"]), "date": r["disclosed_at"][:10]}
    return out


def _reports_from_index(idx: pd.DataFrame) -> dict[str, dict[str, dict]]:
    out: dict[str, dict[str, dict]] = {}
    for r in idx.sort_values("filing_date").to_dict("records"):
        c = tdnet.classify_title(r["title"])
        if c["kind"] != "earnings_report" or c["is_correction"]:
            continue
        ym = orders.period_end_of_title(r["title"])
        if ym:
            out.setdefault(r["code"], {})[ym] = {"disclosure_id": "1401" + r["doc_id"],
                                                 "disclosed_at": f"{r['filing_date']}T00:00",
                                                 "n_q": orders._quarter_of_title(r["title"]), "date": r["filing_date"]}
    return out


def _period_of(reps: dict[str, dict], date: str) -> tuple[str, dict] | None:
    """説明資料の日付(〜3日前まで)の決算短信の期"""
    lo = (pd.Timestamp(date) - pd.Timedelta(days=3)).strftime("%Y-%m-%d")
    c = [(ym, r) for ym, r in reps.items() if lo <= r["date"] <= date]
    return max(c, key=lambda x: x[1]["date"]) if c else None


def _pdf_text(content: bytes) -> str:
    import pypdfium2 as pdfium
    try:
        d = pdfium.PdfDocument(content)
        return "\f".join(d[i].get_textpage().get_text_range() for i in range(len(d))).replace("\r\n", "\n")
    except Exception:
        return ""


def _local_text(path: Path) -> str:
    from .pres_orders import text_of
    return text_of(path)


def collect(code: str, pres: list[dict], reps: dict[str, dict], hq: dict, text_fn) -> dict | None:
    """その会社の説明資料 [{doc, date}] を読んで merge_tables の結果(+docs)"""
    tables, docs = [], {}
    for p in pres:
        per = _period_of(reps, p["date"])
        if not per:
            continue
        ym, rep = per
        text = text_fn(p)
        if not text or not re.search(r"受注|売上", text):
            continue
        for t in parse_text(text, rep["n_q"], ym, hq):
            tables.append((p["date"], t))
            docs.setdefault(ym, p["doc"])
    if not tables:
        return None
    m = merge_tables(tables)
    m["docs"] = docs
    return m


def build_local(data_dir: Path = DATA_DIR, codes: list[str] | None = None, since: str = "2025-01-01") -> dict:
    """手元の説明資料(~/kessan-pdfs)と、取り込み済みの開示一覧の説明資料(TDnet から取得)から、全社分"""
    from .datasources import irbank_api
    idx = irbank_api.load_index()
    idx = idx[(idx["doc_id"] != "") & (idx["source"] == "tdnet")]
    if codes:
        idx = idx[idx["code"].isin(codes)]
    reps = _reports_from_index(idx)
    disc = disclosures.load(data_dir)
    for c, v in _reports_from_disc(disc).items():
        reps.setdefault(c, {}).update(v)
    hq = _hq(data_dir)
    kind = idx["title"].map(lambda t: tdnet.classify_title(t)["kind"])
    pres = idx[(kind == "earnings_presentation") & (idx["filing_date"] >= since)]
    by_code: dict[str, list[dict]] = {}
    for p in pres.to_dict("records"):
        f = PDF_DIR / p["code"] / f"{p['doc_id']}.pdf"
        if f.exists():
            by_code.setdefault(p["code"], []).append({"doc": p["doc_id"], "date": p["filing_date"], "path": f})
    dp = disc[disc["kind"] == "earnings_presentation"]
    if codes:
        dp = dp[dp["code"].isin(codes)]
    for p in dp.to_dict("records"):
        doc = p["disclosure_id"][4:]
        lst = by_code.setdefault(p["code"], [])
        if not any(x["doc"] == doc for x in lst):
            lst.append({"doc": doc, "date": p["disclosed_at"][:10], "url": p["pdf_url"]})
    session = None

    def text_fn(p):
        nonlocal session
        if "path" in p:
            return _local_text(p["path"])
        cache = PDF_DIR / "pres_text" / f"{p['doc']}.txt"
        if cache.exists():
            return cache.read_text(encoding="utf-8")
        from .config import load_config
        from .datasources.http import RateLimitedSession
        session = session or RateLimitedSession.from_config(load_config())
        try:
            resp = session.get(p["url"])
        except Exception:
            return ""
        t = _pdf_text(resp.content) if resp is not None else ""
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(t, encoding="utf-8")
        return t

    merged = {}
    for code, pl in sorted(by_code.items()):
        if code not in reps:
            continue
        m = collect(code, pl, reps[code], hq.get(code, {}), text_fn)
        if m:
            merged[code] = m
    res = apply(merged, reps, data_dir)
    log.info("説明資料の推移表: %s", res)
    return res


def update(cfg: dict, data_dir: Path = DATA_DIR) -> int:
    """TDnet に出た新しい説明資料から(GitHub Actions でも動く)。前の期の値は手元で作った記録を使う"""
    from .datasources.http import RateLimitedSession
    disc = disclosures.load(data_dir)
    reps = _reports_from_disc(disc)
    hq = _hq(data_dir)
    checked = _load_checked(data_dir)
    n0 = len(checked)
    pres = disc[disc["kind"] == "earnings_presentation"].sort_values("disclosed_at").tail(300)
    session = None
    merged, ids = {}, set()
    for p in pres.to_dict("records"):
        if p["disclosure_id"] in checked or p["code"] not in reps:
            continue
        per = _period_of(reps[p["code"]], p["disclosed_at"][:10])
        if not per:
            continue
        session = session or RateLimitedSession.from_config(cfg)
        try:
            resp = session.get(p["pdf_url"])
        except Exception as e:
            log.warning("説明資料の取得失敗 %s: %s", p["code"], e)
            continue
        checked.add(p["disclosure_id"])
        if resp is None:
            continue
        doc = p["disclosure_id"][4:]
        m = collect(p["code"], [{"doc": doc, "date": p["disclosed_at"][:10], "content": resp.content}],
                    reps[p["code"]], hq.get(p["code"], {}), lambda x: _pdf_text(x["content"]))
        if m:
            prev = merged.get(p["code"])
            merged[p["code"]] = m if not prev else merge_tables([("0", _flat(prev)), ("1", _flat(m))]) | {
                "docs": {**prev["docs"], **m["docs"]}}
            ids.add(per[1]["disclosure_id"])
    res = apply(merged, reps, data_dir, only_ids=ids) if merged else {"breakdowns": 0, "orders": 0}
    if len(checked) != n0:
        _checked_path(data_dir).write_text("".join(f"{x}\n" for x in sorted(checked)), encoding="utf-8")
    log.info("説明資料の推移表: %s", res)
    return res["breakdowns"] + res["orders"]


def _flat(m: dict) -> dict:
    """merge_tables の結果を、もう一度 merge_tables に渡せる表の形に戻す"""
    t = {"q": m["q"], "title": m.get("title"), "unit": m["unit"]}
    for kind in ("sales", "orders", "backlog"):
        if kind in m:
            t[kind] = {"total": m[kind]["total"], "items": [(n, m[kind]["items"][n]) for n in m[kind]["names"]]}
    return t


def _checked_path(data_dir: Path) -> Path:
    return data_dir / "pres_tables_checked.txt"


def _load_checked(data_dir: Path) -> set[str]:
    p = _checked_path(data_dir)
    return set(p.read_text(encoding="utf-8").split()) if p.exists() else set()
