"""docs/ の静的サイト(GitHub Pages)を生成する。

- index.html: TDnet 決算速報(開示日を選んで決算・修正を一覧。data/index.json を1分ごとに確認して自動更新)
- calendar.html: 翌営業日〜N営業日先の決算予定
上昇確度は score_of() に集約。決算短信は3つの型(earnings.py)への該当数ごとの過去の上昇率。
"""
import hashlib
import unicodedata
import json
import re
from datetime import date
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from . import calendar_fetch, consensus, disclosures, earnings, history, irdocs, order_adjust, orders, reaction, revisions, segments, universe
from .config import DATA_DIR, ROOT, now_jst

DOCS_DIR = ROOT / "docs"
# 決算速報に出す開示: 決算短信と、業績・配当予想の修正
KIND_LABEL = {"earnings_report": "決算", "forecast_revision": "業績", "dividend_revision": "配当",
              "forecast_dividend_revision": "業績 配当"}
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


def _man(v) -> int | None:
    """百万円(百万円未満は切り捨て)。円の整数にしてから割るので浮動小数の誤差で1ずれない"""
    if v is None or (isinstance(v, float) and v != v) or v == "":
        return None
    n = round(float(v))
    return n // 1000000 if n >= 0 else -(-n // 1000000)


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
    def growth_of(q, f, months):
        """その四半期の値と、months か月前の四半期の値との増減率(3か月の値どうし)"""
        b = quarters.get(_ym_shift(q["end"], -months)) or {}
        return _growth(q.get(f), b.get(f))

    table = [{"label": _period_name(q["end"]), **{f: _man(q.get(f)) for f in FIN_FIELDS},
              # YoY は前年の同じ四半期、QoQ は前の四半期と比べる(EPS は株数が同じなので純利益と同じ)
              **{f"{f}_yoy": growth_of(q, f, 12) for f in FIN_FIELDS},
              **{f"{f}_qoq": growth_of(q, f, 3) for f in FIN_FIELDS},
              # EPS = 四半期の純利益 ÷ 株数(株数は直近の短信の 累計純利益 ÷ 累計EPS。過去の四半期も今の株数で換算)
              "eps": None if not shares or q.get("net") is None else round(q["net"] / shares, 1)}
             for q in qs]
    for t in table:
        t["eps_yoy"], t["eps_qoq"] = t["net_yoy"], t["net_qoq"]
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
    """一覧に出す数値。値と YoY は決算短信1ページ目の連結経営成績(期首からの累計・前年同期比)。
    QoQ は3か月(四半期単独)の値どうしの前四半期比(EPS は純利の QoQ)。売上〜純利は百万円、EPS は円。"""
    if not arch:
        return None
    n_q, end = int(float(arch["n_q"])), str(arch["period_end"])[:7]
    qs = {q["end"]: q for q in hist}
    cum = {f: _f0(arch.get(f"cum_{f}")) for f in FIN_FIELDS}
    prior = {f: _f0(arch.get(f"prior_{f}")) for f in FIN_FIELDS}

    def single(c, f, ym, nq):
        """ym 期末の四半期の単独値 = その時点の累計 c − 同じ期の前の四半期の単独値の合計"""
        if c is None:
            return None
        if nq == 1:
            return c
        prevs = [(qs.get(_ym_shift(ym, -3 * k)) or {}).get(f) for k in range(1, nq)]
        return None if any(v is None for v in prevs) else c - sum(prevs)

    out = {}
    for f in FIN_FIELDS:
        # 値と YoY は短信1ページ目の連結(期首からの累計・前年同期比)。QoQ は3か月の値どうし(前の四半期の数値があるとき)
        q = single(cum[f], f, end, n_q)
        out[f] = {"v": _mil(cum[f]), "yoy": _growth(cum[f], prior[f]),
                  "qoq": None if q is None else _growth(q, (qs.get(_ym_shift(end, -3)) or {}).get(f))}
    ce, pe = _f0(arch.get("cum_eps")), _f0(arch.get("prior_eps"))
    out["eps"] = {"v": None if ce is None else round(ce, 2), "yoy": _growth(ce, pe), "qoq": out["net"]["qoq"]}
    return out


def order_numbers(rec: dict | None, past: list[dict], adj: dict | None = None) -> dict | None:
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

    def qm(ym, name, which="cur"):
        """3か月の値(百万円)。説明資料で1の誤差を直した値があればそれ"""
        v = single(ym, name, which)
        if which == "cur" and name is not None:
            fixed = (adj or {}).get(re.sub(r"\s|・", "", unicodedata.normalize("NFKC", name)), {}).get(ym)
            if fixed is not None:
                return fixed
        return _man(v)

    def nums(s, name):
        b = {"v": _man(s.get("backlog")), "yoy": s.get("backlog_yoy"), "qoq": None}
        q = qm(end, name) if n_q and end else None
        if q is None:
            o = {"v": _man(s.get("orders")), "yoy": s.get("orders_yoy"), "qoq": None, "cum": True}
        else:
            pe = _ym_shift(end, -3)
            o = {"v": q, "yoy": _growth(q, qm(end, name, "prior")), "qoq": _growth(q, qm(pe, name))}
        if n_q and end:
            ps = seg_of(by_end.get(_ym_shift(end, -3)), name)
            # 受注残高: 前の四半期末と比べる(1Qは短信の表にある前期末の残高でもよい)
            pb = ps.get("backlog") if ps else (s.get("backlog_fy") if n_q == 1 else None)
            b["qoq"] = _growth(s.get("backlog"), pb)
        return {"orders": o, "backlog": b}

    d = rec["data"]
    return {"total": nums(d["total"], None),
            "segments": [{"name": s["name"], **({"parent": s["parent"]} if s.get("parent") else {}),
                          **nums(s, re.sub(r"\s", "", s["name"]))} for s in d["segments"]],
            "hist": order_history(by_end, end, n=5),          # 受注の推移は1年前(前年の同じ四半期)から今回まで(5四半期)
            "seg_hist": [{"name": sg["name"], **({"parent": sg["parent"]} if sg.get("parent") else {}),
                          "rows": order_history(by_end, end, n=5, seg=re.sub(r"\s", "", sg["name"]),
                                                adj=(adj or {}).get(re.sub(r"\s|・", "", unicodedata.normalize("NFKC", sg["name"])), {}))}
                         for sg in d["segments"]]}


def order_history(by_end: dict, upto: str, n: int = 20, seg: str | None = None, adj: dict | None = None) -> list[dict]:
    """受注の推移(古い順・最大 n 四半期): 期間・受注高(累計)と YoY・3か月の値(単体Q)と QoQ・QYoY・受注残と YoY。
    seg を渡すとそのセグメント(品目)の推移(名前は空白を除いて照合)。"""
    def tot(ym):
        r = by_end.get(ym)
        if not r:
            return {}
        if seg is None:
            return r["data"].get("total") or {}
        return next((x for x in r["data"].get("segments", []) if re.sub(r"\s", "", x["name"]) == seg), None) or {}

    def nq(ym):
        r = by_end.get(ym)
        return int(float(r["n_q"])) if r and r.get("n_q") else None

    def single(ym):
        c, q = tot(ym).get("orders"), nq(ym)
        if c is None or q is None:
            return None
        if q == 1:
            return c
        b = tot(_ym_shift(ym, -3)).get("orders")
        return None if b is None or nq(_ym_shift(ym, -3)) != q - 1 else c - b

    out = []
    for ym in sorted(k for k in by_end if k and k <= upto)[-n:]:
        t, q = tot(ym), nq(ym)
        if q is None or (t.get("orders") is None and t.get("backlog") is None):
            continue
        fy = _ym_shift(ym, 3 * (4 - q))
        cum, py = t.get("orders"), tot(_ym_shift(ym, -12))
        sq = single(ym)
        # 説明資料で1の誤差を直した値(百万円)があればそれ
        sqm = lambda y: (adj or {})[y] if y in (adj or {}) else _man(single(y))
        # 前年の記録があれば実数から計算する(短信の「前年同期比」は比率か増減率かが表によって違う)
        yoy = _growth(cum, py.get("orders")) if py.get("orders") is not None else t.get("orders_yoy")
        byoy = _growth(t.get("backlog"), py.get("backlog")) if py.get("backlog") is not None else t.get("backlog_yoy")
        pb = tot(_ym_shift(ym, -3)).get("backlog")
        out.append({"label": f"{fy[2:4]}/{int(fy[5:7])} {q}Q", "cum": _man(cum), "yoy": yoy,
                    "bqoq": _growth(t.get("backlog"), pb),
                    "q": sqm(ym) if sq is not None else None,
                    "qoq": _growth(sqm(ym) if sq is not None else None, sqm(_ym_shift(ym, -3))),
                    "qyoy": _growth(sqm(ym) if sq is not None else None, sqm(_ym_shift(ym, -12))),
                    "backlog": _man(t.get("backlog")), "byoy": byoy})
    return out


def segment_numbers(rec: dict | None, past: list[dict], main: dict | None, ords: dict | None = None) -> dict | None:
    """セグメント別の表: 受注高・受注残高(受注の表がある会社)・売上・利益。
    売上・利益は決算短信のセグメント情報の表そのままの累計と YoY(前年同期比)に加えて、
    単体Q(3か月 = 累計 − 前の四半期までの累計)と QoQ・QYoY(前の四半期の記録があるとき)。合計の行は短信のセグメント表の合計(読めたときだけ)と受注の合計(ords)。
    金額は百万円(百万円未満は切り捨て)。"""
    if not rec and not (ords and ords.get("segments")):
        return None
    key = lambda n: re.sub(r"\s|・", "", unicodedata.normalize("NFKC", n))
    by_end = {str(p["period_end"])[:7]: p for p in past if p.get("period_end")}
    n_q = int(float(rec["n_q"])) if rec and rec.get("n_q") else None
    end = str(rec.get("period_end") or "")[:7] if rec else ""
    if rec and end:
        by_end[end] = rec

    TOT = "__合計__"                                    # 短信の表の合計(一番右の列)

    def seg_at(ym, k):
        r = by_end.get(ym)
        if not r:
            return None
        if k == TOT:
            return r["data"].get("total")
        return next((x for x in r["data"]["segments"] if key(x["name"]) == k), None)

    def nq_at(ym):
        r = by_end.get(ym)
        return int(float(r["n_q"])) if r and r.get("n_q") else None

    def single(ym, k, f, prior=False):
        x, nq = seg_at(ym, k), nq_at(ym)
        c = None if x is None else x.get(f"{f}_prior" if prior else f)
        if c is None or nq is None:
            return None
        if nq == 1:
            return c
        if nq_at(_ym_shift(ym, -3)) != nq - 1:
            return None
        b = seg_at(_ym_shift(ym, -3), k)
        b = None if b is None else b.get(f"{f}_prior" if prior else f)
        if b is None:
            return None
        # 3か月の売上がマイナスになるのは、セグメントの組み替えで前の四半期の表と列が合っていないとき。出さない
        return None if f == "sales" and c - b < 0 else c - b

    def flow(x, k, f):
        """累計と YoY は短信のセグメント情報の表そのまま(期首からの累計・前年同期比)。
        単体Q は3か月(累計 − 前の四半期までの累計)、QoQ は前の四半期の3か月と、QYoY は前年の同じ四半期の3か月と比べる"""
        c, p = x.get(f), x.get(f"{f}_prior")
        ly = _ym_shift(end, -12) if end else None
        if p is None and ly and nq_at(ly) == n_q:      # 短信に前年同期の表が無い会社は前年の短信の記録から
            p = (seg_at(ly, k) or {}).get(f)
        q = single(end, k, f) if n_q and end else None
        b = single(_ym_shift(end, -3), k, f) if q is not None else None
        qp = None
        if q is not None:
            qp = single(end, k, f, prior=True)
            if qp is None:
                qp = single(ly, k, f)
        return {"v": sc(c), "yoy": _growth(c, p), "q": sc(q), "qoq": _growth(q, b), "qyoy": _growth(q, qp),
                "raw": (c, p, q, b, qp)}

    sc = _man                                          # 百万円(百万円未満は切り捨て)

    rows, index = [], {}
    for x in (rec["data"]["segments"] if rec else []):
        k = key(x["name"])
        index[k] = len(rows)
        rows.append({"name": x["name"], "sales": flow(x, k, "sales"), "op": flow(x, k, "op")})
    for o in (ords or {}).get("segments", []):          # 受注の表のセグメント(名前が同じなら同じ行に)
        k = key(o["name"])
        if k in index:
            rows[index[k]].update(orders=o["orders"], backlog=o["backlog"])
            continue
        row = {"name": o["name"], "orders": o["orders"], "backlog": o["backlog"]}
        pk = key(o["parent"]) if o.get("parent") else None
        if pk and pk in index:                          # 品目は事業の行のすぐ下に(字下げ)
            row["child"] = True
            at = index[pk] + 1
            while at < len(rows) and rows[at].get("child"):
                at += 1
            rows.insert(at, row)
            index = {key(r["name"]): i for i, r in enumerate(rows)}
        else:
            index[k] = len(rows)
            rows.append(row)
    total = {}
    t = (rec or {}).get("data", {}).get("total") if rec else None
    if t and (t.get("sales") is not None or t.get("op") is not None):
        # 合計は短信のセグメント表の一番右の列の値そのまま(計上額、無ければ合計)。足し算はしない
        total.update(sales=flow(t, TOT, "sales"), op=flow(t, TOT, "op"), name=t.get("name") or "合計")
    for r in rows + [total]:
        for f in ("sales", "op"):
            if isinstance(r.get(f), dict):
                r[f].pop("raw", None)
    if ords:
        total.update(orders=ords["total"]["orders"], backlog=ords["total"]["backlog"])
    if len(rows) < 2 and not ords:
        return None
    cols = [c for c in ("orders", "backlog", "sales", "op") if any(c in r for r in rows) or c in total]

    def history(k, n=8):
        """そのセグメント(k)の四半期ごとの3か月の売上・利益と YoY(前年の同じ四半期比)・QoQ(前四半期比)。古い順に最大 n 四半期"""
        out = []
        for ym in sorted(y for y in by_end if y and y <= end)[-n:]:
            q = nq_at(ym)
            if q is None or seg_at(ym, k) is None:
                continue
            fy = _ym_shift(ym, 3 * (4 - q))
            row = {"label": f"{fy[2:4]}/{int(fy[5:7])} {q}Q", "end": ym, "q": q}
            for f in ("sales", "op"):
                v = single(ym, k, f)
                p = single(ym, k, f, prior=True)
                if p is None:
                    p = single(_ym_shift(ym, -12), k, f)
                row[f] = {"v": sc(v), "yoy": _growth(v, p), "qoq": _growth(v, single(_ym_shift(ym, -3), k, f))}
            if row["sales"]["v"] is not None or row["op"]["v"] is not None:
                out.append(row)
        return out

    hist = []
    if rec and end:
        for x in rec["data"]["segments"]:
            h = history(key(x["name"]))
            if h:
                hist.append({"name": x["name"], "rows": h})
        if total.get("name"):
            h = history(TOT)
            if h:
                hist.append({"name": total["name"], "rows": h, "total": True})
    return {"cols": cols, "segments": rows, "total": total or None, "unit": "百万円", "hist": hist}


def consensus_panel(snap: dict | None, arch: dict | None) -> dict | None:
    """アナリスト予想の平均(Yahoo Finance)。列: 四半期(3か月)の予想2つと今期・来期の通期、行: 売上(百万円)・EPS(円)。
    取れるのは売上と EPS だけ(営業利益・経常利益・純利益の予想は無い)。
    四半期の名前は、発表前に取った値なら「今回発表の四半期・その次」、発表後なら「次・その次」。"""
    if not snap:
        return None
    n_q = int(float(arch["n_q"])) if arch and arch.get("n_q") else None
    end = str(arch["period_end"])[:7] if arch and arch.get("period_end") else None
    before = bool(snap["before"])
    annual = n_q == 4
    shifted = annual and not before                    # 通期決算のあとに取った値は「今期」が来期にずれている

    def qlabel(k):                                     # k=0: yfinance の 0q、k=1: +1q
        if not n_q:
            return "今四半期" if k == 0 else "次四半期"
        q = n_q + k + (0 if before else 1)
        fy = "今期" if q <= 4 else "来期"
        return f"{fy}{(q - 1) % 4 + 1}Q"

    fy_end = _ym_shift(end, 3 * (4 - n_q)) if end and n_q else None
    next_fy = _ym_shift(fy_end, 12) if fy_end else None
    cols = [(qlabel(0), "0q"), (qlabel(1), "1q"),
            ("今期通期", None if shifted else "0y"), ("来期通期", "0y" if shifted else "1y")]
    rows = []
    for m, label in (("rev", "売上"), ("eps", "EPS")):
        vals = []
        for _, k in cols:
            v = _f0(snap.get(f"{m}_{k}")) if k else None
            vals.append(None if v is None else (round(v, 1) if m == "eps" else _mil(v)))
        if any(v is not None for v in vals):
            rows.append({"label": label, "vals": vals})
    if not rows:
        return None
    return {"date": str(snap["fetched_at"])[:10], "before": before, "n": _f0(snap.get("n_0y")),
            "next_fy": f"{next_fy[:4]}/{next_fy[5:7]}" if next_fy else "", "cols": [c for c, _ in cols], "rows": rows}


_MARKET_MARK = re.compile(r"^[GPQAFSN]-")


def company_name(name: str, listed: bool = False) -> str:
    """一覧に出す会社名: 全角英数を半角に(幅を詰める)し、TDnet が頭に付ける市場の印(G-=グロース、P-=TOKYO PRO Market、
    Q-=福証Q-Board、A-=札証アンビシャス など)を外す。プライム・スタンダード(listed)の会社は社名そのものなので外さない"""
    n = unicodedata.normalize("NFKC", name)
    return n if listed else _MARKET_MARK.sub("", n)


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
    ord_adj = order_adjust.load(data_dir)["adj"]
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

    # 「この決算より前」は期末で決める(1Qと2Qを同じ日に出す会社がある: 発表日では前後が分からない)
    def end_of(r):
        a = arch.get(r["disclosure_id"])
        return str(a["period_end"])[:7] if a and a.get("period_end") else None

    def before(r, qs, include_self=False):
        e = end_of(r)
        if e is None:
            return [q for q in qs if q["announced"] < r["disclosed_at"][:10]]
        return [q for q in qs if q["end"] < e or (include_self and q["end"] == e)]

    def earlier(r, recs):
        e = end_of(r)
        return [o for o in recs if o["disclosure_id"] != r["disclosure_id"] and
                (str(o.get("period_end") or "")[:7] < e if e and o.get("period_end") else o["disclosed_at"] < r["disclosed_at"])]

    rows = []
    listed = set(universe.load(data_dir)["code"])     # プライム・スタンダード
    for r in disc.sort_values(["disclosed_at", "code"], ascending=[False, True]).to_dict("records"):
        p = score_of(r, stats, cfg, feats)
        f = feats.get(r["disclosure_id"])
        rows.append({"id": r["disclosure_id"], "date": r["disclosed_at"][:10], "time": r["disclosed_at"][11:16],
                     "code": r["code"], "name": company_name(r["name"], r["code"] in listed),
                     "kind": kind_label(r, f),
                     "group": "決算" if r["kind"] == "earnings_report" else "修正",
                     # 種別に出す文字: 決算 / 業績(業績予想の修正) / 配当(配当予想の修正) / 業績 配当(両方を横に並べる)
                     "label": KIND_LABEL.get(r["kind"], "修正"),
                     "period": period_label(r, f),
                     "url": r["pdf_url"], "pdfs": pdf_links(
                         r, presentations, ir_docs,
                         manual={k: v for k, v in manual.items()
                                 if k[1] or latest_earn.get(r["code"]) == r["disclosed_at"]}),
                     "title": r["title"],
                     "nums": quarter_numbers(arch.get(r["disclosure_id"]), before(r, hist.get(r["code"], [])))
                     if r["kind"] == "earnings_report" else revision_numbers(revs.get(r["disclosure_id"])),
                     "ord": order_numbers(ord_by_id.get(r["disclosure_id"]), earlier(r, ords.get(r["code"], [])),
                                          ord_adj.get(r["code"])),
                     "fin": fin_panel(r, arch.get(r["disclosure_id"]) if r["kind"] == "earnings_report" else None,
                                      before(r, hist.get(r["code"], []), include_self=True)
                                      if r["kind"] == "earnings_report" else hist.get(r["code"], []),
                                      shares=shares_at(r["code"], r["disclosed_at"])),
                     "pct": None if p is None else round(p * 100),
                     "up": p is not None and round(p * 100) > 50})
        rows[-1]["seg"] = segment_numbers(seg_by_id.get(r["disclosure_id"]),
                                          earlier(r, segs.get(r["code"], [])),
                                          rows[-1]["nums"], rows[-1]["ord"]) if r["kind"] == "earnings_report" else None
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
                                            day_json=dump(latest).replace("</", "<\\/"),
                                            proxy=(cfg.get("site") or {}).get("tdnet_proxy", ""))),
        "calendar.html": ("calendar.html.j2", dict(page="calendar", groups=_group(calendar_rows(data_dir)))),
    }
    for name, (tpl, ctx) in pages.items():
        p = docs_dir / name
        p.write_text(env.get_template(tpl).render(generated_at=now, **ctx), encoding="utf-8")
        out.append(str(p))
    return out
