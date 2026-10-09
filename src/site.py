"""docs/ の静的サイト(GitHub Pages)を生成する。

- index.html: 翌営業日〜N営業日先の決算予定(universe × calendar)+ 直近の業績修正
- disclosures.html: 適時開示ウォッチ
judge / report(M3/M4)ができたら index.html は判定一覧に置き換える。
"""
import json
from pathlib import Path

import pandas as pd
from jinja2 import Environment, FileSystemLoader

from . import calendar_fetch, disclosures, disclosures_page, universe
from .bizdays import next_business_day
from .config import DATA_DIR, ROOT, now_jst

DOCS_DIR = ROOT / "docs"


def upcoming_rows(cfg: dict, data_dir: Path = DATA_DIR) -> list[dict]:
    cal = calendar_fetch.load(data_dir)
    uni = universe.load(data_dir)
    if cal.empty or uni.empty:
        return []
    df = cal.merge(uni[["code", "name", "market", "sector33", "avg_turnover_20d"]], on="code", how="inner")
    disc = disclosures.load(data_dir)
    coverage = disclosures.load_meta(data_dir).get("coverage_start")
    fcfg = cfg["factors"]["revision_recency"]
    today = now_jst().date()

    rows = []
    for r in df.itertuples():
        rr = disclosures.revision_recency(r.code, today, disc, fcfg, coverage)
        rows.append({
            "date": r.announce_date, "code": r.code, "name": r.name, "market": r.market,
            "sector": r.sector33 if isinstance(r.sector33, str) else "",
            "fq": r.fiscal_q, "timing": r.announce_timing, "time": r.announce_time,
            "confirmed": r.time_confirmed == "true",
            "turnover_oku": None if pd.isna(r.avg_turnover_20d) else round(r.avg_turnover_20d / 1e8, 1),
            "rev_dir": rr["last_direction"], "rev_pct": rr["last_change_pct"], "rev_date": rr["last_date"],
        })
    return rows


def render_index(cfg: dict, data_dir: Path = DATA_DIR, docs_dir: Path = DOCS_DIR) -> Path:
    rows = upcoming_rows(cfg, data_dir)
    now = now_jst()
    cal = calendar_fetch.load(data_dir)
    env = Environment(loader=FileSystemLoader(ROOT / "templates"), autoescape=True)
    html = env.get_template("index.html.j2").render(
        data_json=json.dumps(rows, ensure_ascii=False).replace("</", "<\\/"),
        generated_at=now.strftime("%Y-%m-%d %H:%M"),
        calendar_updated=cal["updated_at"].iloc[0] if not cal.empty else "-",
        next_bd=next_business_day(now.date()).isoformat(),
        min_turnover_oku=cfg["judge"]["min_avg_turnover"] / 1e8,
        has_universe=universe.path(data_dir).exists(),
    )
    docs_dir.mkdir(parents=True, exist_ok=True)
    out = docs_dir / "index.html"
    out.write_text(html, encoding="utf-8")
    return out


def render_all(cfg: dict, data_dir: Path = DATA_DIR, docs_dir: Path = DOCS_DIR) -> list[str]:
    return [str(render_index(cfg, data_dir, docs_dir)), str(disclosures_page.render(data_dir, docs_dir))]
