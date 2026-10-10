"""docs/ の静的サイト(GitHub Pages)を生成する。

- index.html: TDnet 決算速報(開示日を選んで決算・修正を一覧。data/index.json を1分ごとに確認して自動更新)
- calendar.html: 翌営業日〜N営業日先の決算予定
上昇確度は score_of() に集約。決算短信は3つの型(earnings.py)への該当数ごとの過去の上昇率。
"""
import hashlib
import json
import re
from datetime import date
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from . import calendar_fetch, consensus, disclosures, earnings, history, irdocs, orders, reaction, revisions, segments, universe
from .config import DATA_DIR, ROOT, now_jst

DOCS_DIR = ROOT / "docs"
# 決算速報に出す開示: 決算短信と、業績・配当予想の修正
FEED_KINDS = {"earnings_report", "forecast_revision", "forecast_dividend_revision", "dividend_revision"}


def kind_label(r: dict, feat: dict | None = None) -> str:
    k, d, t = r["kind"], r["direction"], r["title"]
    arrow = {"up": "↑", "down": "↓"}.get(d, "")
    if k == "earnings_report":
        return "決算"
    if k == "forecast_revision":
        return f"修正{arrow}"
    if k == "forecast_dividend_revision":
        return f"修正{arrow}" + ("増配" if "増配" in t else "減配" if "減配" in t else "配当")
    if k == "dividend_revision":
        return "増配" if "増配" in t or d == "up" else "減配" if ("減配" in t or "無配" in t or d == "down") else "配当"
    if k == "forecast_initial":
        return "予想"
    return k


_Q_IN_TITLE = [(re.compile(r"第\s*[1１一]\s*四半期"), "1Q"), (re.compile(r"第\s*[2２二]\s*四半期|中間期"), "2Q"),
               (re.compile(r"第\s*[3３三]\s*四半期"), "3Q")]


def period_label(r: dict, feat: dict | None = None) -> str:
    """決算: 1Q/2Q/3Q/通期。修正: 通期/中間。配当: 中間/期末(表題で分かる場合)。"""
    k, t = r["kind"], r["title"]
    if k == "earnings_report":
        n_q = (feat or {}).get("n_q")
        if n_q not in ("", None):
            return {1: "1Q", 2: "2Q", 3: "3Q", 4: "通期"}[int(float(n_q))]
        for pat, q in _Q_IN_TITLE:
            if pat.search(t):
                return q
        return "通期"
    if k in ("forecast_revision", "forecast_dividend_revision"):
        period = str(r.get("period") or "")
        if period.startswith("CurrentYear") or period.startswith("NextYear"):
            return "通期"
        if period.startswith("CurrentAccumulatedQ2"):
            return "中間"
        if "通期" in t:
            return "通期"
        if "中間" in t or re.search(r"第\s*[2２二]\s*四半期", t):
            return "中間"
        return "通期" if "業績予想" in t else ""
    if k == "dividend_revision":
        return "中間" if "中間配当" in t else "期末" if "期末配当" in t else ""
    return ""


def score_of(r: dict, stats: dict, cfg: dict, feats: dict) -> float | None:
    """上昇確度(0〜1)。決算短信は該当した型の数、修正・配当は方向と幅ごとの、過去の上昇率(縮小推定)。"""
    rc = cfg["reaction"]
    return reaction.probability(reaction.bucket(r, rc["large_change_pct"], feats), stats, rc["prior_n"])


def _f(v):
    return None if v in ("", None) else float(v)


def _pct_item(label: str, v, unit: str = "%") -> list:
    """[ラベル, 表示, トーン]"""
    v = _f(v)
    if v is None:
        return [label, "–", ""]
    if v >= 999:
        return [label, "黒字転換", "up"]
    return [label, f"{v:+.1f}{unit}", "up" if v > 0 else "down" if v < 0 else ""]


METRIC_LABELS = [("Sales|Revenue", "売上"), ("Operating(Income|Profit)", "営業益"), ("Ordinary", "経常益"),
                 ("ProfitAttributable|NetIncome|Profit", "純利益")]


def details(r: dict, feat: dict | None) -> list[list]:
    """行をタップしたときに出す数値。"""
    if r["kind"] == "earnings_report":
        if not feat or feat.get("status") != "ok":
            return [["内容", "XBRLなし" if (feat or {}).get("status") == "no_xbrl" else "集計中", ""]]
        n_q = int(_f(feat["n_q"]) or 0)
        span = "単Q" if n_q == 1 else "累計"
        items = [_pct_item(f"売上({span}YoY)", feat["sales_ytd_yoy"]),
                 _pct_item(f"営業益({span}YoY)", feat["op_ytd_yoy"]),
                 _pct_item("売上の加速", feat["accel"], "pt"),
                 _pct_item("営業利益率の変化", feat["margin_delta"], "pt")]
        if _f(feat["progress"]) is not None:
            std = n_q * 25
            pr = _f(feat["progress"])
            items.append(["進捗率(営業益)", f"{pr:.0f}%(標準{std}%)", "up" if pr > std + 5 else "down" if pr < std - 5 else ""])
        if _f(feat["sales_qoq"]) is not None:
            items += [_pct_item("売上(前四半期比)", feat["sales_qoq"]), _pct_item("営業益(前四半期比)", feat["op_qoq"])]
        if _f(feat["conservatism"]) is not None:
            c = _f(feat["conservatism"])
            items.append(["予想の慎重度", f"{c:.2f}(1未満=慎重)", "up" if c <= 0.85 else ""])
        items.append(["今回の予想修正", {"True": "あり", "False": "なし"}.get(feat["revised"], "–"), ""])
        names = {"①": "①リクルート", "②": "②キオクシア", "③": "③ローツェB"}
        types = feat.get("types") or ""
        items.append(["該当した型", " ".join(names[m] for m in types if m in names) or "なし", "up" if types else ""])
        return items
    if r["kind"] in ("forecast_revision", "forecast_dividend_revision") and r.get("xbrl_changes"):
        ch = json.loads(r["xbrl_changes"])
        span = "中間" if str(r.get("period", "")).startswith("CurrentAccumulatedQ2") else "通期"
        items, used = [], set()
        for pat, label in METRIC_LABELS:
            k = next((k for k in ch if re.search(pat, k) and k not in used), None)
            if k is not None:
                used.add(k)
                items.append(_pct_item(f"{span}{label}(修正率)", ch[k]))
        return items
    return []


def pdf_links(r: dict, presentations: dict[str, list[dict]], ir_docs: dict[str, dict] | None = None,
              within_days: int = 14, manual: dict | None = None) -> list[dict]:
    """行に並べるPDF。決算: 短信 + 決算説明資料(TDnet で短信と同日〜within_days日以内、最大2件。
    TDnet に無ければ各社IRサイトで見つけたもの)。修正: 修正。"""
    if r["kind"] != "earnings_report":
        return [{"label": "修正", "url": r["pdf_url"]}]
    links = [{"label": "短信", "url": r["pdf_url"]}]
    d0 = date.fromisoformat(r["disclosed_at"][:10])
    near = [p for p in presentations.get(r["code"], [])
            if 0 <= (date.fromisoformat(p["disclosed_at"][:10]) - d0).days <= within_days]
    for i, p in enumerate(sorted(near, key=lambda p: p["disclosed_at"])[:2]):
        links.append({"label": "資料" if i == 0 else "資料2", "url": p["pdf_url"], "title": p["title"]})
    m = manual or {}
    hand = m.get((r["code"], r["disclosed_at"][:10])) or m.get((r["code"], ""))
    if hand:                                        # 手で登録した資料を最優先
        links[1:] = [{"label": "資料", "url": hand["url"], "title": hand.get("title") or "説明資料(手動登録)"}]
        return links
    ir = (ir_docs or {}).get(r.get("disclosure_id", ""))
    if not near and ir and ir.get("url"):
        links.append({"label": "資料", "url": ir["url"], "title": f"{ir.get('title', '')}(会社のIRサイト)"})
    return links


FIN_FIELDS = ["sales", "op", "ordinary", "net"]
FIN_LABELS = {"sales": "売上", "op": "営業益", "ordinary": "経常益", "net": "純利益"}


def _ym_shift(ym: str, months: int) -> str:
    y, m = int(ym[:4]), int(ym[5:7])
    i = y * 12 + (m - 1) + months
    return f"{i // 12}-{i % 12 + 1:02d}"


def _period_name(end_ym: str) -> str:
    """'2026-06' -> '26.04-06'(3ヵ月の決算期)"""
    start = _ym_shift(end_ym, -2)
    return f"{start[2:4]}.{start[5:7]}-{end_ym[5:7]}"


def _yen(v) -> int | None:
    return None if v is None or (isinstance(v, float) and v != v) or v == "" else round(float(v))


def _mil(v) -> int | None:
    return None if v is None or (isinstance(v, float) and v != v) or v == "" else round(float(v) / 1e6)


def fin_panel(r: dict, arch: dict | None, hist: list[dict], n_quarters: int = 8,
              shares: float | None = None) -> dict | None:
    """行を開いたときの業績表。quarters: 直近の単独四半期、plan: 今期の累計実績と会社予想・進捗率(百万円)。"""
    quarters = {q["end"]: dict(q) for q in hist}
    plan = None
    if arch:
        n_q, end = int(float(arch["n_q"])), str(arch["period_end"])[:7]
        prev = [quarters.get(_ym_shift(end, -3 * k)) for k in range(1, n_q)]
        cum = {f: (None if arch.get(f"cum_{f}") in ("", None) else float(arch[f"cum_{f}"])) for f in FIN_FIELDS}
        if all(p is not None for p in prev):            # 今回の四半期の単独値 = 累計 − 同じ期の前の四半期
            quarters[end] = {"end": end, **{f: None if cum[f] is None or any(p.get(f) is None for p in prev)
                                            else cum[f] - sum(p[f] for p in prev) for f in FIN_FIELDS}}
        fy_end = _ym_shift(end, 3 * (4 - n_q))
        ends = [_ym_shift(fy_end, -3 * (4 - k)) for k in range(1, 5)]   # 今期の1Q〜4Qの期末
        cols = []
        for k, e in enumerate(ends, 1):
            if k == n_q:
                cols.append(cum)
            elif k < n_q:
                qs = [quarters.get(x) for x in ends[:k]]
                cols.append({f: None if any(q is None or q.get(f) is None for q in qs) else sum(q[f] for q in qs)
                             for f in FIN_FIELDS})
            else:
                cols.append({f: None for f in FIN_FIELDS})
        fc = {f: (None if arch.get(f"fc_{f}") in ("", None) else float(arch[f"fc_{f}"])) for f in FIN_FIELDS}
        next_year = str(arch.get("fc_next_year")) == "True"
        rows = []
        for f in FIN_FIELDS:
            prog = None if next_year or fc[f] in (None, 0) or cum[f] is None or fc[f] < 0                 else round(cum[f] / fc[f] * 100, 1)
            rows.append([FIN_LABELS[f]] + [_mil(c[f]) for c in cols] + [_mil(fc[f]), prog])
        plan = {"fy": f"{fy_end[:4]}年{int(fy_end[5:7])}月期", "n_q": n_q,
                "fc_label": "来期予想" if next_year else "会社予想", "rows": rows}
    qs = sorted(quarters.values(), key=lambda q: q["end"])[-n_quarters:]
    # 金額は円のまま(百万円に丸めない)
    table = [{"label": _period_name(q["end"]), **{f: _yen(q.get(f)) for f in FIN_FIELDS},
              # EPS = 四半期の純利益 ÷ 株数(株数は直近の短信の 累計純利益 ÷ 累計EPS。過去の四半期も今の株数で換算)
              "eps": None if not shares or q.get("net") is None else round(q["net"] / shares, 1)}
             for q in qs]
    if not table and not plan:
        return None
    return {"quarters": table, "plan": plan}


def _f0(v):
    return None if v in ("", None) or (isinstance(v, float) and v != v) else float(v)


def _growth(cur, base):
    """前年比・前四半期比(%)。比べる相手が赤字のときも (今回 − 前回) ÷ |前回| で数値にする。前回がゼロなら出さない。"""
    if cur is None or base is None or base == 0:
        return None
    return round((cur - base) / abs(base) * 100, 1)


def revision_numbers(rv: dict | None) -> dict | None:
    """修正の行: 値=修正後の予想、YoY=前期実績比、QoQ欄=前回予想からの修正率"""
    if not rv or rv.get("period") in ("", "-"):
        return None
    out = {}
    for m in ["sales", "op", "ordinary", "net", "eps"]:
        cur, prev, prior = (_f0(rv.get(f"{w}_{m}")) for w in ("cur", "prev", "prior"))
        v = None if cur is None else (round(cur, 2) if m == "eps" else _mil(cur))
        out[m] = {"v": v, "yoy": _growth(cur, prior), "qoq": _growth(cur, prev)}
    return out


def quarter_numbers(arch: dict | None, hist: list[dict]) -> dict | None:
    """一覧に出す数値。値と YoY は短信1ページ目と同じ累計(前年同期比)。
    QoQ は単独四半期(3か月)どうしの前四半期比(EPS は純利の QoQ)。売上〜純利は百万円、EPS は円。"""
    if not arch:
        return None
    n_q, end = int(float(arch["n_q"])), str(arch["period_end"])[:7]
    qs = {q["end"]: q for q in hist}
    cum = {f: _f0(arch.get(f"cum_{f}")) for f in FIN_FIELDS}
    prior = {f: _f0(arch.get(f"prior_{f}")) for f in FIN_FIELDS}
    prev_in_fy = [qs.get(_ym_shift(end, -3 * k)) for k in range(1, n_q)]

    def single(c, prevs, f):
        if c is None:
            return None
        if n_q == 1:
            return c
        if any(p is None or p.get(f) is None for p in prevs):
            return None
        return c - sum(p[f] for p in prevs)

    out = {}
    for f in FIN_FIELDS:
        cur_q = single(cum[f], prev_in_fy, f)              # QoQ 用の単独四半期
        prv = (qs.get(_ym_shift(end, -3)) or {}).get(f)
        # 値と YoY は短信1ページ目と同じ累計(前年同期比)
        out[f] = {"v": _mil(cum[f]), "yoy": _growth(cum[f], prior[f]), "qoq": _growth(cur_q, prv)}
    ce, pe = _f0(arch.get("cum_eps")), _f0(arch.get("prior_eps"))
    out["eps"] = {"v": None if ce is None else round(ce, 2), "yoy": _growth(ce, pe), "qoq": out["net"]["qoq"]}
    return out


def order_numbers(rec: dict | None, past: list[dict]) -> dict | None:
    """受注高・受注残高(百万円)と YoY・QoQ。
    受注高は説明資料と同じ3か月(四半期単独)の値 = 累計 − 前の四半期までの累計。
    YoY は前年の同じ四半期(短信の前年同期の累計からも同じ引き算)、QoQ は前の四半期の3か月の値と比べる。
    前の四半期の短信の記録が無いときだけ、短信どおりの累計(cum=True)を出す。
    受注残高は期末の残高どうしで比べる。past はこの短信より前の同じ会社の受注の記録。"""
    if not rec:
        return None
    by_end = {str(p["period_end"])[:7]: p for p in past if p.get("period_end")}
    n_q = int(float(rec["n_q"])) if rec.get("n_q") else None
    end = str(rec.get("period_end") or "")[:7]
    if end:
        by_end[end] = rec

    def seg_of(r, name):
        if not r:
            return None
        d = r["data"]
        if name is None:
            return d["total"]
        return next((s for s in d["segments"] if re.sub(r"\s", "", s["name"]) == name), None)

    def cum(ym, name, which):
        """その四半期末の累計受注高。which=prior は前年同期の累計(短信の前年同期比から戻す)"""
        s = seg_of(by_end.get(ym), name)
        if s is None or s.get("orders") is None:
            return None
        if which == "cur":
            return s["orders"]
        if s.get("orders_prior") is not None:
            return s["orders_prior"]
        y = s.get("orders_yoy")
        return None if y is None or y <= -100 else s["orders"] / (1 + y / 100)

    def nq_at(ym):
        r = by_end.get(ym)
        return int(float(r["n_q"])) if r and r.get("n_q") else None

    def single(ym, name, which):
        nq, c = nq_at(ym), cum(ym, name, which)
        if nq is None or c is None:
            return None
        if nq == 1:
            return c
        if nq_at(_ym_shift(ym, -3)) != nq - 1:
            return None
        before = cum(_ym_shift(ym, -3), name, which)
        return None if before is None else c - before

    def nums(s, name):
        b = {"v": _mil(s.get("backlog")), "yoy": s.get("backlog_yoy"), "qoq": None}
        q = single(end, name, "cur") if n_q and end else None
        if q is None:
            o = {"v": _mil(s.get("orders")), "yoy": s.get("orders_yoy"), "qoq": None, "cum": True}
        else:
            pe = _ym_shift(end, -3)
            o = {"v": _mil(q), "yoy": _growth(q, single(end, name, "prior")), "qoq": _growth(q, single(pe, name, "cur"))}
        if n_q and end:
            ps = seg_of(by_end.get(_ym_shift(end, -3)), name)
            # 受注残高: 前の四半期末と比べる(1Qは短信の表にある前期末の残高でもよい)
            pb = ps.get("backlog") if ps else (s.get("backlog_fy") if n_q == 1 else None)
            b["qoq"] = _growth(s.get("backlog"), pb)
        return {"orders": o, "backlog": b}

    d = rec["data"]
    return {"total": nums(d["total"], None),
            "segments": [{"name": s["name"], **nums(s, re.sub(r"\s", "", s["name"]))} for s in d["segments"]]}


def segment_numbers(rec: dict | None, past: list[dict], main: dict | None) -> dict | None:
    """セグメント別の売上・利益(百万円)と YoY・QoQ。値と YoY は累計(前年同期比)、
    QoQ は3か月単独どうし(累計 − 前の四半期までの累計)で、前の四半期の短信の記録があるときだけ。
    合計の行は一覧と同じ数値(main = quarter_numbers の結果)。"""
    if not rec:
        return None
    by_end = {str(p["period_end"])[:7]: p for p in past if p.get("period_end")}
    n_q = int(float(rec["n_q"])) if rec.get("n_q") else None
    end = str(rec.get("period_end") or "")[:7]

    def seg_of(r, key):
        return next((s for s in r["data"]["segments"] if re.sub(r"\s", "", s["name"]) == key), None) if r else None

    def single(ym, nq, cum, key, f):
        if cum is None or nq is None:
            return None
        if nq == 1:
            return cum
        before = seg_of(by_end.get(_ym_shift(ym, -3)), key)
        return None if before is None or before.get(f) is None else cum - before[f]

    def nums(s):
        key = re.sub(r"\s", "", s["name"])
        out = {}
        for f in ("sales", "op"):
            o = {"v": _mil(s.get(f)), "yoy": _growth(s.get(f), s.get(f"{f}_prior")), "qoq": None}
            if n_q and end:
                pe = _ym_shift(end, -3)
                prev = by_end.get(pe)
                ps = seg_of(prev, key)
                if ps:
                    pn = int(float(prev["n_q"])) if prev.get("n_q") else None
                    o["qoq"] = _growth(single(end, n_q, s.get(f), key, f), single(pe, pn, ps.get(f), key, f))
            out[f] = o
        return out

    total = {"sales": (main or {}).get("sales"), "op": (main or {}).get("op")} if main else None
    return {"segments": [{"name": s["name"], **nums(s)} for s in rec["data"]["segments"]], "total": total}


def consensus_panel(snap: dict | None, arch: dict | None) -> dict | None:
    """アナリスト予想の平均(通期)と、会社予想(通期の決算なら実績)との差。売上は百万円、EPS は円。
    通期決算のあとに取った値は「今期」が来期にずれているので、来期として扱う。"""
    if not snap:
        return None
    n_q = int(float(arch["n_q"])) if arch and arch.get("n_q") else None
    annual = n_q == 4
    shifted = annual and not snap["before"]
    cons = {"this": None if shifted else ("0y", snap.get("n_0y")),
            "next": ("0y", snap.get("n_0y")) if shifted else ("1y", snap.get("n_1y"))}

    def company(term, m):
        if not arch:
            return None
        if term == "this":
            if annual:
                return _f0(arch.get("cum_eps")) if m == "eps" else _f0(arch.get("cum_sales"))
            return None if m == "eps" or str(arch.get("fc_next_year")) == "True" else _f0(arch.get("fc_sales"))
        return _f0(arch.get("fc_sales")) if annual and m == "rev" else None

    rows = []
    for m, label in (("rev", "売上"), ("eps", "EPS")):
        row = {"label": label}
        for term in ("this", "next"):
            c = cons[term]
            cv = _f0(snap.get(f"{m}_{c[0]}")) if c else None
            co = company(term, m)
            fmt = (lambda v: None if v is None else round(v, 1)) if m == "eps" else _mil
            row[term] = {"cons": fmt(cv), "co": fmt(co), "diff": _growth(co, cv)}
        rows.append(row)
    return {"date": str(snap["fetched_at"])[:10], "before": bool(snap["before"]), "annual": annual,
            "n": _f0(snap.get("n_0y")), "rows": rows}


def feed_rows(cfg: dict, data_dir: Path = DATA_DIR) -> list[dict]:
    """取り込み済みの全期間の決算・修正(新しい順)。"""
    all_disc = disclosures.load(data_dir)
    presentations: dict[str, list[dict]] = {}
    for p in all_disc[all_disc["kind"] == "earnings_presentation"].to_dict("records"):
        presentations.setdefault(p["code"], []).append(p)
    disc = all_disc[all_disc["kind"].isin(FEED_KINDS) & (all_disc["is_correction"] != "True")]
    stats = reaction.load_stats(data_dir)
    feats = earnings.by_disclosure(data_dir)
    ir_docs = irdocs.found_by_disclosure(data_dir)
    manual = irdocs.load_manual(data_dir)
    # 日付なしの手動登録は、その会社の一番新しい決算にだけ付ける
    latest_earn = disc[disc["kind"] == "earnings_report"].groupby("code")["disclosed_at"].max().to_dict()
    arch = {a["disclosure_id"]: a for a in earnings.load_archive(data_dir).to_dict("records")}
    hist = history.by_code(history.load(data_dir))
    revs = revisions.by_disclosure(data_dir)
    ords = orders.by_code(data_dir)
    for recs in ords.values():                     # 受注を読んだ時点で期がまだ分からなかった短信は、ここで補う
        for o in recs:
            a = arch.get(o["disclosure_id"])
            if a and not o["period_end"]:
                o["n_q"], o["period_end"] = a["n_q"], a["period_end"]
    ord_by_id = {o["disclosure_id"]: o for recs in ords.values() for o in recs}
    segs = segments.by_code(data_dir)
    for recs in segs.values():
        for o in recs:
            a = arch.get(o["disclosure_id"])
            if a and not o["period_end"]:
                o["n_q"], o["period_end"] = a["n_q"], a["period_end"]
    seg_by_id = {o["disclosure_id"]: o for recs in segs.values() for o in recs}
    cons = consensus.by_code(data_dir)
    # 株数(EPS の換算用): 短信の 累計純利益 ÷ 累計EPS。その開示の時点で一番新しい短信のもの
    shares_hist: dict[str, list[tuple[str, float]]] = {}
    for a in sorted(arch.values(), key=lambda a: str(a["disclosed_at"])):
        net, eps = _f0(a.get("cum_net")), _f0(a.get("cum_eps"))
        if net and eps:
            shares_hist.setdefault(a["code"], []).append((str(a["disclosed_at"]), net / eps))

    def shares_at(code, at):
        xs = [v for t, v in shares_hist.get(code, []) if t <= at] or [v for _, v in shares_hist.get(code, [])]
        return xs[-1] if xs else None

    rows = []
    for r in disc.sort_values(["disclosed_at", "code"], ascending=[False, True]).to_dict("records"):
        p = score_of(r, stats, cfg, feats)
        f = feats.get(r["disclosure_id"])
        rows.append({"id": r["disclosure_id"], "date": r["disclosed_at"][:10], "time": r["disclosed_at"][11:16],
                     "code": r["code"], "name": r["name"], "kind": kind_label(r, f),
                     "group": "決算" if r["kind"] == "earnings_report" else "修正",
                     "period": period_label(r, f),
                     "url": r["pdf_url"], "pdfs": pdf_links(
                         r, presentations, ir_docs,
                         manual={k: v for k, v in manual.items()
                                 if k[1] or latest_earn.get(r["code"]) == r["disclosed_at"]}),
                     "title": r["title"],
                     "nums": quarter_numbers(arch.get(r["disclosure_id"]),
                                             [q for q in hist.get(r["code"], []) if q["announced"] < r["disclosed_at"][:10]])
                     if r["kind"] == "earnings_report" else revision_numbers(revs.get(r["disclosure_id"])),
                     "ord": order_numbers(ord_by_id.get(r["disclosure_id"]),
                                          [o for o in ords.get(r["code"], []) if o["disclosed_at"] < r["disclosed_at"]]),
                     "fin": fin_panel(r, arch.get(r["disclosure_id"]) if r["kind"] == "earnings_report" else None,
                                      [q for q in hist.get(r["code"], []) if q["announced"] <= r["disclosed_at"][:10]
                                       or r["kind"] != "earnings_report"],
                                      shares=shares_at(r["code"], r["disclosed_at"])),
                     "pct": None if p is None else round(p * 100),
                     "up": p is not None and round(p * 100) > 50})
        rows[-1]["seg"] = segment_numbers(seg_by_id.get(r["disclosure_id"]),
                                          [o for o in segs.get(r["code"], []) if o["disclosed_at"] < r["disclosed_at"]],
                                          rows[-1]["nums"]) if r["kind"] == "earnings_report" else None
        rows[-1]["cons"] = consensus_panel(consensus.snapshot(cons.get(r["code"], []), r["disclosed_at"]),
                                           arch.get(r["disclosure_id"])) if r["kind"] == "earnings_report" else None
    return rows


def calendar_rows(data_dir: Path = DATA_DIR) -> list[dict]:
    cal, uni = calendar_fetch.load(data_dir), universe.load(data_dir)
    if cal.empty or uni.empty:
        return []
    df = cal.merge(uni[["code", "name", "avg_turnover_20d"]], on="code", how="inner")
    df = df.sort_values(["announce_date", "avg_turnover_20d"], ascending=[True, False])
    return [{"date": r.announce_date, "time": r.announce_time or "引け後", "intraday": r.announce_timing == "場中",
             "code": r.code, "name": r.name, "kind": f"決算{r.fiscal_q}", "pct": None, "up": False}
            for r in df.itertuples()]


def _group(rows: list[dict]) -> list[tuple[str, list[dict]]]:
    out = []
    for r in rows:
        if not out or out[-1][0] != r["date"]:
            out.append((r["date"], []))
        out[-1][1].append(r)
    return out


def _date_label(d: str) -> str:
    x = date.fromisoformat(d)
    return f"{x.month}/{x.day}({'月火水木金土日'[x.weekday()]})"


def build_id() -> str:
    """画面の作り(テンプレートと site.py)が変わると変わるID。開いているタブはこれを見て自動で読み直す。"""
    h = hashlib.sha1()
    for p in sorted((ROOT / "templates").glob("*.j2")) + [Path(__file__)]:
        h.update(p.read_bytes().replace(b"\r\n", b"\n"))     # 改行コードの違い(Windows/Actions)で変わらないように
    return h.hexdigest()[:10]


def render_all(cfg: dict, data_dir: Path = DATA_DIR, docs_dir: Path = DOCS_DIR) -> list[str]:
    """index.html(決算速報)・開示日ごとの data/YYYY-MM-DD.json・data/index.json・calendar.html を出力。"""
    env = Environment(loader=FileSystemLoader(ROOT / "templates"), autoescape=True)
    env.filters["dlabel"] = _date_label
    now = now_jst().strftime("%Y-%m-%d %H:%M:%S")
    data_out = docs_dir / "data"
    data_out.mkdir(parents=True, exist_ok=True)

    by_date: dict[str, list[dict]] = {}
    for r in feed_rows(cfg, data_dir):
        by_date.setdefault(r["date"], []).append(r)
    dates = sorted(by_date, reverse=True)
    dump = lambda o: json.dumps(o, ensure_ascii=False, separators=(",", ":"))  # noqa: E731
    out = []
    for d, rows in by_date.items():
        p = data_out / f"{d}.json"
        body = dump({"date": d, "rows": rows})
        if not p.exists() or p.read_text(encoding="utf-8") != body:     # 変わった日だけ書き換える
            p.write_text(body, encoding="utf-8")
            out.append(str(p))
    days = [{"date": d, "n": len(by_date[d]),
             "earn": sum(r["group"] == "決算" for r in by_date[d]),
             "rev": sum(r["group"] == "修正" for r in by_date[d])} for d in dates]
    index = {"generated_at": now, "build": build_id(), "manual": irdocs.manual_hash(data_dir),
             "dates": dates, "days": days}
    (data_out / "index.json").write_text(dump(index), encoding="utf-8")
    old_feed = docs_dir / "feed.json"
    if old_feed.exists():
        old_feed.unlink()

    latest = {"date": dates[0], "rows": by_date[dates[0]]} if dates else {"date": "", "rows": []}
    pages = {
        "index.html": ("feed.html.j2", dict(page="feed", index_json=dump(index).replace("</", "<\\/"),
                                            day_json=dump(latest).replace("</", "<\\/"))),
        "calendar.html": ("calendar.html.j2", dict(page="calendar", groups=_group(calendar_rows(data_dir)))),
    }
    for name, (tpl, ctx) in pages.items():
        p = docs_dir / name
        p.write_text(env.get_template(tpl).render(generated_at=now, **ctx), encoding="utf-8")
        out.append(str(p))
    return out
