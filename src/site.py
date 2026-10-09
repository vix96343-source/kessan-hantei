"""docs/ の静的サイト(GitHub Pages)を生成する。

- index.html: 適時開示フィード(時刻 / コード / 企業名 / 種別 / 上昇確度)
- calendar.html: 翌営業日〜N営業日先の決算予定
上昇確度は score_of() に集約。ユーザー提供の「上がりやすい決算内容」基準が来たら差し替える。
現状は暫定で過去の同種開示の反応率(reaction.py)を使う。
"""
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from . import calendar_fetch, disclosures, reaction, universe
from .config import DATA_DIR, ROOT, now_jst

DOCS_DIR = ROOT / "docs"
FEED_KINDS = {"earnings_report", "forecast_revision", "forecast_dividend_revision",
              "dividend_revision", "forecast_initial"}


def kind_label(r: dict) -> str:
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


def score_of(r: dict, stats: dict, cfg: dict) -> float | None:
    """上昇確度(0〜1)。暫定: 同種開示の過去反応率。"""
    rc = cfg["reaction"]
    return reaction.probability(reaction.bucket(r, rc["large_change_pct"]), stats, rc["prior_n"])


def feed_rows(cfg: dict, data_dir: Path = DATA_DIR) -> list[dict]:
    disc = disclosures.load(data_dir)
    disc = disc[disc["kind"].isin(FEED_KINDS) & (disc["is_correction"] != "True")]
    stats = reaction.load_stats(data_dir)
    days = sorted(disc["disclosed_at"].str[:10].unique(), reverse=True)[:cfg["site"]["feed_days"]]
    disc = disc[disc["disclosed_at"].str[:10].isin(days)]
    rows = []
    for r in disc.sort_values(["disclosed_at", "code"], ascending=[False, True]).to_dict("records"):
        p = score_of(r, stats, cfg)
        rows.append({"date": r["disclosed_at"][:10], "time": r["disclosed_at"][11:16], "code": r["code"],
                     "name": r["name"], "kind": kind_label(r), "title": r["title"], "url": r["pdf_url"],
                     "pct": None if p is None else round(p * 100), "up": p is not None and p >= 0.5})
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
