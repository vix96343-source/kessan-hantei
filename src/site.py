"""docs/ の静的サイト(GitHub Pages)を生成する。

- index.html: TDnet 決算速報(開示日を選んで決算・修正を一覧。data/index.json を1分ごとに確認して自動更新)
- calendar.html: 翌営業日〜N営業日先の決算予定
上昇確度は score_of() に集約。決算短信は3つの型(earnings.py)への該当数ごとの過去の上昇率。
"""
import json
import re
from datetime import date
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from . import calendar_fetch, disclosures, earnings, reaction, universe
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


def pdf_links(r: dict, presentations: dict[str, list[dict]], within_days: int = 7) -> list[dict]:
    """行に並べるPDF。決算: 短信 + 決算説明資料(短信と同日〜within_days日以内、最大2件)。修正: 修正。"""
    if r["kind"] != "earnings_report":
        return [{"label": "修正", "url": r["pdf_url"]}]
    links = [{"label": "短信", "url": r["pdf_url"]}]
    d0 = date.fromisoformat(r["disclosed_at"][:10])
    near = [p for p in presentations.get(r["code"], [])
            if 0 <= (date.fromisoformat(p["disclosed_at"][:10]) - d0).days <= within_days]
    for i, p in enumerate(sorted(near, key=lambda p: p["disclosed_at"])[:2]):
        links.append({"label": "資料" if i == 0 else "資料2", "url": p["pdf_url"], "title": p["title"]})
    return links


def feed_rows(cfg: dict, data_dir: Path = DATA_DIR) -> list[dict]:
    """取り込み済みの全期間の決算・修正(新しい順)。"""
    all_disc = disclosures.load(data_dir)
    presentations: dict[str, list[dict]] = {}
    for p in all_disc[all_disc["kind"] == "earnings_presentation"].to_dict("records"):
        presentations.setdefault(p["code"], []).append(p)
    disc = all_disc[all_disc["kind"].isin(FEED_KINDS) & (all_disc["is_correction"] != "True")]
    stats = reaction.load_stats(data_dir)
    feats = earnings.by_disclosure(data_dir)
    rows = []
    for r in disc.sort_values(["disclosed_at", "code"], ascending=[False, True]).to_dict("records"):
        p = score_of(r, stats, cfg, feats)
        f = feats.get(r["disclosure_id"])
        rows.append({"id": r["disclosure_id"], "date": r["disclosed_at"][:10], "time": r["disclosed_at"][11:16],
                     "code": r["code"], "name": r["name"], "kind": kind_label(r, f),
                     "group": "決算" if r["kind"] == "earnings_report" else "修正",
                     "period": period_label(r, f),
                     "url": r["pdf_url"], "pdfs": pdf_links(r, presentations),
                     "title": r["title"], "details": details(r, f),
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
    index = {"generated_at": now, "dates": dates, "days": days}
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
