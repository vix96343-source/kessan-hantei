"""会社の決算月(四半期業績の表の「27/3 2Q」のため)。

短信の記録(期末と何Q)も開示の題名の「2027年３月期」も無い会社(業績修正・配当の開示だけの会社)は、
IRBANK の四半期業績のページ(期が「2026/09-2Q」の形)から決算月を取り、data/fy_months.csv に保存する。
"""
from __future__ import annotations

import logging
import re
import unicodedata
from datetime import datetime
from pathlib import Path

import pandas as pd

from .config import DATA_DIR

log = logging.getLogger(__name__)
COLUMNS = ["code", "fy_month", "fetched_at"]
_TITLE = re.compile(r"\d{4}年\s*(\d{1,2})月期")


def path(data_dir: Path = DATA_DIR) -> Path:
    return Path(data_dir) / "fy_months.csv"


def load(data_dir: Path = DATA_DIR) -> dict[str, int]:
    p = path(data_dir)
    if not p.exists():
        return {}
    df = pd.read_csv(p, dtype=str).fillna("")
    return {r["code"]: int(r["fy_month"]) for r in df.to_dict("records") if r["fy_month"].isdigit()}


def title_month(title: str) -> int | None:
    m = _TITLE.search(unicodedata.normalize("NFKC", str(title)))
    return int(m.group(1)) if m and 1 <= int(m.group(1)) <= 12 else None


def month_of_quarters(quarters: list[dict]) -> int | None:
    """IRBANK の四半期業績(period「2026/09-2Q」)の一番新しい期の決算月"""
    for q in reversed(quarters):
        m = re.match(r"\d{4}/(\d{2})-[1-4]Q", q.get("period", ""))
        if m:
            return int(m.group(1))
    return None


def needed(data_dir: Path = DATA_DIR) -> list[str]:
    """表に出る開示(決算・修正・配当)の会社で、短信の記録・題名・保存済みのどれからも決算月が分からない会社"""
    from . import disclosures, earnings, orders
    from .site import FEED_KINDS
    disc = disclosures.load(data_dir)
    known = set(load(data_dir))
    for df in (earnings.load_archive(data_dir), orders.load(data_dir)):      # 期末と何Qがそろった記録のある会社
        ok = df["period_end"].astype(str).str.match(r"\d{4}-\d{2}") & pd.to_numeric(df["n_q"], errors="coerce").between(1, 4)
        known |= set(df.loc[ok, "code"])
    try:
        done = set(pd.read_csv(path(data_dir), dtype=str)["code"])
    except (FileNotFoundError, KeyError):
        done = set()
    known |= {c for c, t in zip(disc["code"], disc["title"]) if title_month(t)}
    codes = disc[disc["kind"].isin(FEED_KINDS)].sort_values("disclosed_at", ascending=False)["code"]
    return [c for c in dict.fromkeys(codes) if c not in known and c not in done]


def update(cfg: dict, data_dir: Path = DATA_DIR, budget: int = 20) -> int:
    """決算月の分からない会社を IRBANK から最大 budget 社(1秒1件)。取れなかった会社も空で記録して取り直さない"""
    from .datasources import irbank
    codes = needed(data_dir)[:budget]
    if not codes:
        return 0
    session = irbank.session_from_config(cfg)
    rows = []
    for code in codes:
        try:
            m = month_of_quarters(irbank.fetch_quarterly(session, code))
        except Exception as e:                          # IRBANK が 500 を返す会社がある(9444)。毎回取り直さない
            log.warning("決算月: %s の取得に失敗: %s", code, e)
            m = None
        rows.append({"code": code, "fy_month": "" if m is None else str(m),
                     "fetched_at": datetime.now().isoformat(timespec="seconds")})
    if rows:
        p = path(data_dir)
        old = pd.read_csv(p, dtype=str) if p.exists() else pd.DataFrame(columns=COLUMNS)
        out = pd.concat([old[~old["code"].isin([r["code"] for r in rows])], pd.DataFrame(rows)]).reindex(columns=COLUMNS)
        out.sort_values("code").to_csv(p, index=False, encoding="utf-8")
    log.info("決算月: %d 社", len(rows))
    return len(rows)
