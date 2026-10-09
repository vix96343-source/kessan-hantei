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

from . import calendar_fetch, disclosures, earnings, history, irdocs, reaction, universe
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


def _mil(v) -> int | None:
    return None if v is None or (isinstance(v, float) and v != v) or v == "" else round(float(v) / 1e6)


def fin_panel(r: dict, arch: dict | None, hist: list[dict], n_quarters: int = 8) -> dict | None:
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
    table = [{"label": _period_name(q["end"]), **{f: _mil(q.get(f)) for f in FIN_FIELDS},
              "margin": None if not q.get("sales") or q.get("op") is None else round(q["op"] / q["sales"] * 100, 1)}
             for q in qs]
    if not table and not plan:
        return None
    return {"quarters": table, "plan": plan}


def _f0(v):
    return None if v in ("", None) or (isinstance(v, float) and v != v) else float(v)


def _growth(cur, base):
    return None if cur is None or base is None or base <= 0 else round((cur / base - 1) * 100, 1)


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
                     "title": r["title"], "details": details(r, f),
                     "nums": quarter_numbers(arch.get(r["disclosure_id"]) if r["kind"] == "earnings_report" else None,
                                             [q for q in hist.get(r["code"], []) if q["announced"] < r["disclosed_at"][:10]]),
                     "fin": fin_panel(r, arch.get(r["disclosure_id"]) if r["kind"] == "earnings_report" else None,
                                      [q for q in hist.get(r["code"], []) if q["announced"] <= r["disclosed_at"][:10]
                                       or r["kind"] != "earnings_report"]),
                     "pct": None if p is None else round(p * 100),
                     "up": p is not None and round(p * 100) > 50})
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
