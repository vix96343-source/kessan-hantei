"""disclosures.csv から docs/disclosures.html(適時開示ウォッチ)を生成する。"""
import json
from datetime import timedelta
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from . import disclosures
from .config import DATA_DIR, ROOT, now_jst

DOCS_DIR = ROOT / "docs"
PAGE_COLUMNS = ["disclosed_at", "code", "name", "title", "kind", "is_correction",
                "direction", "change_pct", "metric", "period", "pdf_url"]


def summary(df, today) -> dict:
    recent = df[df["disclosed_at"].str[:10] > (today - timedelta(days=7)).isoformat()]
    fc = recent[recent["kind"].isin(["forecast_revision", "forecast_dividend_revision"])
                & (recent["is_correction"] != "True")]
    return {
        "up7": int((fc["direction"] == "up").sum()),
        "down7": int((fc["direction"] == "down").sum()),
        "er7": int((recent["kind"] == "earnings_report").sum()),
        "pr7": int((recent["kind"] == "earnings_presentation").sum()),
    }


def render(data_dir: Path = DATA_DIR, docs_dir: Path = DOCS_DIR) -> Path:
    df = disclosures.load(data_dir).sort_values(["disclosed_at", "disclosure_id"], ascending=False)
    now = now_jst()
    data_json = json.dumps(df[PAGE_COLUMNS].to_dict("records"), ensure_ascii=False).replace("</", "<\\/")
    env = Environment(loader=FileSystemLoader(ROOT / "templates"), autoescape=True)
    html = env.get_template("disclosures.html.j2").render(
        data_json=data_json,
        s=summary(df, now.date()),
        generated_at=now.strftime("%Y-%m-%d %H:%M"),
        coverage_start=disclosures.load_meta(data_dir).get("coverage_start", "-"),
    )
    docs_dir.mkdir(parents=True, exist_ok=True)
    out = docs_dir / "disclosures.html"
    out.write_text(html, encoding="utf-8")
    return out
