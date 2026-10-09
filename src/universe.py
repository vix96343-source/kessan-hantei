"""M1: プライム・スタンダード銘柄の一覧と20日平均売買代金 → data/universe.csv"""
import logging
from pathlib import Path

import pandas as pd

from .config import DATA_DIR, now_jst
from .datasources import jpx, yf
from .datasources.http import RateLimitedSession

log = logging.getLogger(__name__)

COLUMNS = ["code", "name", "market", "sector33", "avg_turnover_20d", "updated_at"]
MARKETS = ("プライム", "スタンダード")


def path(data_dir: Path = DATA_DIR) -> Path:
    return data_dir / "universe.csv"


def load(data_dir: Path = DATA_DIR) -> pd.DataFrame:
    p = path(data_dir)
    if not p.exists():
        return pd.DataFrame(columns=COLUMNS)
    df = pd.read_csv(p, dtype={"code": str})
    return df.reindex(columns=COLUMNS)


def target(listed: pd.DataFrame) -> pd.DataFrame:
    # 内国株式の普通株のみ(外国株式・優先株など5桁コードは除外)
    return listed[listed["market"].isin(MARKETS) & listed["market_raw"].str.contains("内国株式")
                  & listed["code"].str.fullmatch(r"[0-9A-Z]{4}")]


def build(listed: pd.DataFrame, turnover: dict[str, float], updated_at: str,
          previous: dict[str, float] | None = None) -> pd.DataFrame:
    """今回取れなかった銘柄の売買代金は前回値を引き継ぐ(レート制限で全滅しても劣化しない)。"""
    u = target(listed).copy()
    merged = {**(previous or {}), **turnover}
    u["avg_turnover_20d"] = u["code"].map(merged).round(0)
    u["updated_at"] = updated_at
    return u[COLUMNS].sort_values("code").reset_index(drop=True)


def update(cfg: dict, data_dir: Path = DATA_DIR, session: RateLimitedSession | None = None) -> dict:
    session = session or RateLimitedSession.from_config(cfg)
    listed = jpx.fetch_listed(session)
    save_listed(listed, data_dir)
    codes = target(listed)["code"].tolist()
    turnover = yf.avg_turnover(codes, days=cfg["universe"]["turnover_days"])
    prev = load(data_dir).dropna(subset=["avg_turnover_20d"]).set_index("code")["avg_turnover_20d"].to_dict()
    u = build(listed, turnover, now_jst().strftime("%Y-%m-%dT%H:%M"), prev)
    data_dir.mkdir(parents=True, exist_ok=True)
    u.to_csv(path(data_dir), index=False, encoding="utf-8")
    stats = {"rows": len(u), "with_turnover": int(u["avg_turnover_20d"].notna().sum()),
             **u["market"].value_counts().to_dict()}
    log.info("universe: %s", stats)
    return stats


NON_EQUITY = r"ETF|ETN|REIT|インフラ|ベンチャー|出資証券|カントリー"


def listed_path(data_dir: Path = DATA_DIR) -> Path:
    return data_dir / "listed.csv"


def save_listed(listed: pd.DataFrame, data_dir: Path = DATA_DIR) -> None:
    """全上場銘柄の市場・商品区分(ETF・REIT 等を見分けるため)"""
    listed[["code", "name", "market_raw"]].to_csv(listed_path(data_dir), index=False, encoding="utf-8")


def non_equity_codes(data_dir: Path = DATA_DIR) -> set[str]:
    """ETF・ETN・REIT・インフラファンド等(株式会社の決算ではないもの)"""
    p = listed_path(data_dir)
    if not p.exists():
        return set()
    df = pd.read_csv(p, dtype=str, keep_default_na=False)
    return set(df.loc[df["market_raw"].str.contains(NON_EQUITY), "code"])
