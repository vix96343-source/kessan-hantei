"""M2: 翌営業日〜N営業日先の決算発表予定 → data/calendar.csv(毎回全置換)

一次ソースは JPX 決算発表予定日(各社が取引所に届け出た日付)。
発表時刻は未取得のため announce_timing=引け後 / time_confirmed=false を既定とし、
TDnet に当日の決算短信が出ていればその時刻で確定させる。
"""
import logging
from datetime import date
from pathlib import Path

import pandas as pd

from . import disclosures
from .bizdays import add_business_days
from .config import DATA_DIR, now_jst
from .datasources import jpx
from .datasources.http import RateLimitedSession

log = logging.getLogger(__name__)

COLUMNS = ["announce_date", "code", "announce_time", "announce_timing", "time_confirmed",
           "fiscal_q", "source", "updated_at"]


def path(data_dir: Path = DATA_DIR) -> Path:
    return data_dir / "calendar.csv"


def load(data_dir: Path = DATA_DIR) -> pd.DataFrame:
    p = path(data_dir)
    if not p.exists():
        return pd.DataFrame(columns=COLUMNS)
    return pd.read_csv(p, dtype=str, keep_default_na=False).reindex(columns=COLUMNS, fill_value="")


def timing_of(hhmm: str, cutoff: str) -> str:
    return "場中" if hhmm and hhmm < cutoff else "引け後"


def build(schedule: pd.DataFrame, today: date, horizon: int, cutoff: str,
          disc: pd.DataFrame, updated_at: str) -> pd.DataFrame:
    start, end = add_business_days(today, 1), add_business_days(today, horizon)
    s = schedule[(schedule["announce_date"] >= start) & (schedule["announce_date"] <= end)].copy()
    s["announce_date"] = s["announce_date"].map(lambda d: d.isoformat())

    # 既にTDnetに短信が出ている(前倒し発表など)銘柄は実際の時刻で確定
    er = disc[disc["kind"] == "earnings_report"].assign(date=lambda x: x["disclosed_at"].str[:10],
                                                        time=lambda x: x["disclosed_at"].str[11:16])
    actual = er.drop_duplicates(["code", "date"]).set_index(["code", "date"])["time"].to_dict()
    times = [actual.get((c, d), "") for c, d in zip(s["code"], s["announce_date"])]
    s["announce_time"] = times
    s["time_confirmed"] = [str(bool(t)).lower() for t in times]
    s["announce_timing"] = [timing_of(t, cutoff) for t in times]
    s["source"] = "jpx"
    s["updated_at"] = updated_at
    return s[COLUMNS].sort_values(["announce_date", "code"]).reset_index(drop=True)


def update(cfg: dict, data_dir: Path = DATA_DIR, session: RateLimitedSession | None = None,
           today: date | None = None) -> dict:
    ccfg = cfg["calendar"]
    session = session or RateLimitedSession.from_config(cfg)
    today = today or now_jst().date()
    schedule = jpx.fetch_schedule(session)
    cal = build(schedule, today, ccfg["horizon_business_days"], ccfg["intraday_cutoff"],
                disclosures.load(data_dir), now_jst().strftime("%Y-%m-%dT%H:%M"))
    data_dir.mkdir(parents=True, exist_ok=True)
    cal.to_csv(path(data_dir), index=False, encoding="utf-8")
    stats = {"rows": len(cal), "schedule_rows": len(schedule),
             "by_date": cal["announce_date"].value_counts().sort_index().to_dict()}
    log.info("calendar: %s", stats)
    return stats
