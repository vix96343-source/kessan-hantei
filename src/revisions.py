"""業績予想の修正(TDnet の修正XBRL)から、修正後の予想・前回予想・前期実績を data/revisions.csv にためる。
決算速報の「修正」の行に、値(修正後の予想)・YoY(前期実績比)・修正率(前回予想比)を出すのに使う。"""
import json
import logging
from pathlib import Path

import pandas as pd

from . import disclosures
from .config import DATA_DIR
from .datasources import tdnet
from .datasources.http import RateLimitedSession

log = logging.getLogger(__name__)

METRICS = ["sales", "op", "ordinary", "net", "eps"]
# periods: 修正した全部の期(中間期・通期など)の {期: {cur, prev, prior}} の JSON(一覧には修正率の大きい期を出す)
COLUMNS = ["disclosure_id", "period"] + [f"{w}_{m}" for w in ("cur", "prev", "prior") for m in METRICS] + ["periods"]


def path(data_dir: Path = DATA_DIR) -> Path:
    return data_dir / "revisions.csv"


def load(data_dir: Path = DATA_DIR) -> pd.DataFrame:
    p = path(data_dir)
    if not p.exists():
        return pd.DataFrame(columns=COLUMNS)
    return pd.read_csv(p, dtype={"disclosure_id": str}, keep_default_na=False).reindex(columns=COLUMNS, fill_value="")


def by_disclosure(data_dir: Path = DATA_DIR) -> dict[str, dict]:
    return {r["disclosure_id"]: r for r in load(data_dir).to_dict("records")}


def update(cfg: dict, data_dir: Path = DATA_DIR, session: RateLimitedSession | None = None,
           limit: int = 300) -> int:
    disc = disclosures.load(data_dir)
    have = set(load(data_dir)["disclosure_id"])
    todo = disc[disc["kind"].isin(["forecast_revision", "forecast_dividend_revision"]) & (disc["xbrl_url"] != "")
                & ~disc["disclosure_id"].isin(have)].head(limit)
    if todo.empty:
        return 0
    session = session or RateLimitedSession.from_config(cfg)
    rows = []
    for r in todo.itertuples():
        try:
            content = tdnet.get_zip(session, r.xbrl_url)   # 取り込みで読んだ zip を使い回す
        except Exception as e:
            log.warning("修正XBRL取得失敗 %s: %s", r.code, e)
            continue
        x = tdnet.parse_revision_values(content) if content is not None else {}
        row = {"disclosure_id": r.disclosure_id, "period": x.get("period", "-"),
               "periods": json.dumps(x.get("periods") or {}, ensure_ascii=False)}
        for w in ("cur", "prev", "prior"):
            for m in METRICS:
                row[f"{w}_{m}"] = (x.get(w) or {}).get(m)
        rows.append(row)
    if rows:
        df = pd.concat([load(data_dir), pd.DataFrame(rows).reindex(columns=COLUMNS)])
        df.to_csv(path(data_dir), index=False, encoding="utf-8")
    log.info("revisions: %d", len(rows))
    return len(rows)
