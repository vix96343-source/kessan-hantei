"""決算予定の銘柄の時価総額(億円) → data/market_caps.csv(手元のみ・local_backfill から)

決算時間サイトで、同じ時刻の枠の中を時価総額の大きい順に並べ、小さい会社(既定 100億円以下)を出さないのに使う。
出所は IRBANK の決算発表スケジュール(https://irbank.net/market/kessan?y=YYYY-MM-DD、日付ごとに1ページ)の
「時価総額」列(セルの sortValue が億円の整数)。IRBANK は Actions からつながらないので手元で取る。
"""
import logging
import re
from datetime import date
from pathlib import Path

import pandas as pd
from bs4 import BeautifulSoup

from . import calendar_fetch
from .config import DATA_DIR, now_jst
from .datasources import irbank

log = logging.getLogger(__name__)

URL = "https://irbank.net/market/kessan?y={date}"
COLUMNS = ["code", "mcap_oku", "as_of", "fetched_at"]
REFRESH_HOURS = 6


def path(data_dir: Path = DATA_DIR) -> Path:
    return data_dir / "market_caps.csv"


def load(data_dir: Path = DATA_DIR) -> pd.DataFrame:
    p = path(data_dir)
    if not p.exists():
        return pd.DataFrame(columns=COLUMNS)
    return pd.read_csv(p, dtype=str, keep_default_na=False).reindex(columns=COLUMNS, fill_value="")


def by_code(data_dir: Path = DATA_DIR) -> dict[str, float]:
    df = load(data_dir)
    return {c: float(v) for c, v in zip(df["code"], df["mcap_oku"]) if v}


def parse(html: str) -> dict[str, int]:
    """スケジュールのページ → {コード: 時価総額(億円)}"""
    out = {}
    for tr in BeautifulSoup(html, "html.parser").select("tr"):
        td = tr.find_all("td")
        if len(td) < 5 or not re.fullmatch(r"[0-9A-Z]{4}", td[0].get_text(strip=True)):
            continue
        m = re.search(r"sortValue:(\d+)", " ".join(td[4].get("class") or []))
        if m:
            out[td[0].get_text(strip=True)] = int(m.group(1))
    return out


def update(cfg: dict, data_dir: Path = DATA_DIR, today: date | None = None) -> dict:
    """決算予定(今日以降)の日付のページを REFRESH_HOURS ごとに読み直す。変わった社数を返す"""
    today = today or now_jst().date()
    old = load(data_dir)
    if len(old) and old["fetched_at"].max() > (now_jst() - pd.Timedelta(hours=REFRESH_HOURS)).strftime("%Y-%m-%dT%H:%M"):
        return {"skipped": True}
    days = sorted(d for d in set(calendar_fetch.load(data_dir)["announce_date"]) if d >= today.isoformat())
    s = irbank.session_from_config(cfg)
    caps = {}
    for d in days:
        r = s.get(URL.format(date=d))
        if r is None:
            continue
        r.encoding = r.apparent_encoding
        caps.update(parse(r.text))
    at = now_jst().strftime("%Y-%m-%dT%H:%M")
    new = pd.DataFrame([{"code": c, "mcap_oku": str(v), "as_of": today.isoformat(), "fetched_at": at}
                        for c, v in caps.items()], columns=COLUMNS)
    # 予定から外れた銘柄の古い値も残す(延期などで戻ってくることがある)
    out = pd.concat([old[~old["code"].isin(new["code"])], new]).sort_values("code")
    out.to_csv(path(data_dir), index=False, encoding="utf-8")
    changed = len(set(zip(new["code"], new["mcap_oku"])) - set(zip(old["code"], old["mcap_oku"])))
    stats = {"days": len(days), "codes": len(caps), "changed": changed}
    log.info("market_caps: %s", stats)
    return stats
