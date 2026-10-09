"""docs/ の静的サイト(GitHub Pages)を生成する。

- index.html: 適時開示フィード(時刻 / コード / 企業名 / 種別 / 上昇確度)
- calendar.html: 翌営業日〜N営業日先の決算予定
上昇確度は score_of() に集約。決算短信は3つの型(earnings.py)への該当数ごとの過去の上昇率。
"""
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from . import calendar_fetch, disclosures, earnings, reaction, universe
from .config import DATA_DIR, ROOT, now_jst

DOCS_DIR = ROOT / "docs"
FEED_KINDS = {"earnings_report", "forecast_revision", "forecast_dividend_revision",
              "dividend_revision", "forecast_initial"}


def kind_label(r: dict, feat: dict | None = None) -> str:
    k, d, t = r["kind"], r["direction"], r["title"]
    arrow = {"up": "↑", "down": "↓"}.get(d, "")
    if k == "earnings_report":
        return "決算" + ((feat or {}).get("types") or "")
    if k == "forecast_revision":
        return f"修正{arrow}"
    if k == "forecast_dividend_revision":
        return f"修正{arrow}" + ("増配" if "増配" in t else "減配" if "減配" in t else "配当")
    if k == "dividend_revision":
        return "増配" if "増配" in t or d == "up" else "減配" if ("減配" in t or "無配" in t or d == "down") else "配当"
    if k == "forecast_initial":
        return "予想"
    return k


def score_of(r: dict, stats: dict, cfg: dict, feats: dict) -> float | None:
    """上昇確度(0〜1)。決算短信は該当した型の数、修正・配当は方向と幅ごとの、過去の上昇率(縮小推定)。"""
    rc = cfg["reaction"]
    return reaction.probability(reaction.bucket(r, rc["large_change_pct"], feats), stats, rc["prior_n"])


def detail(feat: dict | None) -> str:
    """行のツールチップ用: 型判定の根拠"""
    if not feat or feat.get("status") != "ok":
        return {"no_history": "過去の四半期データ不足で型判定なし", "no_xbrl": "XBRLなしで型判定なし"}.get(
            (feat or {}).get("status"), "")
    def p(v, unit="%"):
        return "–" if v in ("", None) else f"{float(v):+.1f}{unit}"
    parts = [f"①加速{p(feat['accel'], 'pt')} 利益率{p(feat['margin_delta'], 'pt')}",
             f"②売上QoQ{p(feat['sales_qoq'])} 営業益QoQ{p(feat['op_qoq'])}",
             f"③慎重度{'–' if feat['conservatism'] in ('', None) else format(float(feat['conservatism']), '.2f')}"
             f" 修正{'なし' if feat['revised'] == 'False' else 'あり' if feat['revised'] == 'True' else '–'}"]
    return " / ".join(parts)


def feed_rows(cfg: dict, data_dir: Path = DATA_DIR) -> list[dict]:
    disc = disclosures.load(data_dir)
    disc = disc[disc["kind"].isin(FEED_KINDS) & (disc["is_correction"] != "True")]
    stats = reaction.load_stats(data_dir)
    feats = earnings.by_disclosure(data_dir)
    days = sorted(disc["disclosed_at"].str[:10].unique(), reverse=True)[:cfg["site"]["feed_days"]]
    disc = disc[disc["disclosed_at"].str[:10].isin(days)]
    rows = []
    for r in disc.sort_values(["disclosed_at", "code"], ascending=[False, True]).to_dict("records"):
        p = score_of(r, stats, cfg, feats)
        f = feats.get(r["disclosure_id"])
        rows.append({"date": r["disclosed_at"][:10], "time": r["disclosed_at"][11:16], "code": r["code"],
                     "name": r["name"], "kind": kind_label(r, f), "url": r["pdf_url"],
                     "title": r["title"] + (f"\n{detail(f)}" if detail(f) else ""),
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
    from datetime import date
    x = date.fromisoformat(d)
    return f"{x.month}/{x.day}({'月火水木金土日'[x.weekday()]})"


def render_all(cfg: dict, data_dir: Path = DATA_DIR, docs_dir: Path = DOCS_DIR) -> list[str]:
    env = Environment(loader=FileSystemLoader(ROOT / "templates"), autoescape=True)
    env.filters["dlabel"] = _date_label
    now = now_jst().strftime("%Y-%m-%d %H:%M")
    docs_dir.mkdir(parents=True, exist_ok=True)
    pages = {
        "index.html": dict(page="feed", groups=_group(feed_rows(cfg, data_dir))),
        "calendar.html": dict(page="calendar", groups=_group(calendar_rows(data_dir))),
    }
    out = []
    for name, ctx in pages.items():
        p = docs_dir / name
        p.write_text(env.get_template("site.html.j2").render(generated_at=now, **ctx), encoding="utf-8")
        out.append(str(p))
    return out
