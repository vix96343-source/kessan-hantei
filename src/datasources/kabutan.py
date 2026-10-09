"""株探 決算ページの「3ヵ月決算【実績】」(単独四半期の業績履歴、直近約8四半期)。

robots.txt の Crawl-delay: 3 に従い、専用セッションで3秒間隔を守る。
取得データは再配布しない(data/cache/ は gitignore、Actions では actions/cache で保持)。
"""
import json
import logging
import re
from datetime import date, datetime
from pathlib import Path

from bs4 import BeautifulSoup

from .http import RateLimitedSession

log = logging.getLogger(__name__)

URL = "https://kabutan.jp/stock/finance?code={code}"
CRAWL_DELAY_SEC = 3.0
FIELDS = ["sales", "op", "ordinary", "net"]


def session_from_config(cfg: dict) -> RateLimitedSession:
    h = cfg["http"]
    return RateLimitedSession(h["user_agent"], max(CRAWL_DELAY_SEC, h["min_interval_sec"]),
                              h["retries"], h["timeout_sec"])


def _num(text: str) -> float | None:
    t = text.replace(",", "").replace("－", "").replace("―", "").strip()
    try:
        return float(t) * 1e6          # 百万円 → 円
    except ValueError:
        return None


def parse_quarterly(html: str) -> list[dict]:
    """[{period: '26.04-06', end: '2026-06', sales, op, ordinary, net, announced: '2026-07-23'}, ...] 古い順"""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("div.fin_quarter_result_d table")
    if table is None:
        return []
    out = []
    for tr in table.select("tbody tr"):
        th = tr.find("th")
        m = re.search(r"(\d{2})\.(\d{2})-(\d{2})", th.get_text() if th else "")
        tds = tr.find_all("td")
        if not m or len(tds) < 7:
            continue
        yy, _, end_mm = m.groups()
        start_mm = int(m.group(2))
        end_year = 2000 + int(yy) + (1 if int(end_mm) < start_mm else 0)
        ann = re.search(r"(\d{2})/(\d{2})/(\d{2})", tds[6].get_text())
        out.append({
            "period": m.group(0),
            "end": f"{end_year}-{end_mm}",
            **{f: _num(td.get_text()) for f, td in zip(FIELDS, tds[:4])},
            "announced": f"20{ann.group(1)}-{ann.group(2)}-{ann.group(3)}" if ann else "",
        })
    return out


def fetch_quarterly(session: RateLimitedSession, code: str) -> list[dict]:
    r = session.get(URL.format(code=code))
    if r is None:
        return []
    r.encoding = "utf-8"
    return parse_quarterly(r.text)


# ---------------------------------------------------------------- キャッシュ

def cache_path(cache_dir: Path, code: str) -> Path:
    return cache_dir / f"{code}.json"


def load_cached(cache_dir: Path, code: str) -> dict | None:
    p = cache_path(cache_dir, code)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def get_quarterly(session: RateLimitedSession, code: str, cache_dir: Path,
                  max_age_days: int, today: date, need_announced_on: str | None = None) -> list[dict]:
    """キャッシュが新しければそれを使う。need_announced_on の決算がまだ載っていない場合も再取得はしない
    (当日分は短信XBRLから計算するため、履歴は前四半期まであれば十分)。"""
    c = load_cached(cache_dir, code)
    if c and (today - date.fromisoformat(c["fetched"][:10])).days <= max_age_days:
        return c["quarters"]
    q = fetch_quarterly(session, code)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path(cache_dir, code).write_text(
        json.dumps({"fetched": datetime.now().isoformat(timespec="seconds"), "quarters": q}, ensure_ascii=False),
        encoding="utf-8")
    return q
