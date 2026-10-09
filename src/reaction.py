"""開示後の株価反応と「上昇確度」。

上昇確度 = 同じバケット(種別×方向×修正幅)の過去開示で、
「開示前の終値 → 開示後最初の終値」が上昇した割合。
件数が少ないバケットは全体の上昇率に寄せる(prior_n 件ぶんの擬似データで縮小推定)。
"""
import json
import logging
from bisect import bisect_left, bisect_right
from pathlib import Path

import pandas as pd

from . import disclosures
from .config import DATA_DIR
from .datasources import yf

log = logging.getLogger(__name__)

REACTION_COLUMNS = ["disclosure_id", "code", "ref_date", "out_date", "ret_pct"]


def reactions_path(data_dir: Path = DATA_DIR) -> Path:
    return data_dir / "reactions.csv"


def stats_path(data_dir: Path = DATA_DIR) -> Path:
    return data_dir / "reaction_stats.json"


# ---------------------------------------------------------------- バケット

def bucket(row: dict, large_pct: float) -> str | None:
    """開示1件を上昇確度の集計単位に分類する。対象外は None。"""
    kind, d, title = row["kind"], row["direction"], row["title"]
    if row.get("is_correction") == "True":
        return None
    pct = abs(float(row["change_pct"])) if row.get("change_pct") else None
    size = "" if pct is None else ("_large" if pct >= large_pct else "_small")
    if kind == "earnings_report":
        return "earnings"
    if kind in ("forecast_revision", "forecast_dividend_revision"):
        if d in ("up", "down"):
            return f"rev_{d}{size}"
        return "rev_unknown"
    if kind == "dividend_revision":
        if "増配" in title or d == "up":
            return "div_up"
        if "減配" in title or "無配" in title or d == "down":
            return "div_down"
        return "div_other"
    if kind == "forecast_initial":
        return "forecast_initial"
    return None


# ---------------------------------------------------------------- 反応の計算

def reaction_of(disclosed_at: str, closes: pd.Series, close_time: str) -> tuple | None:
    """(ref_date, out_date, ret_pct)。開示後の終値がまだ無ければ None。"""
    d = pd.Timestamp(disclosed_at[:10]).date()
    t = disclosed_at[11:16]
    days = list(closes.index)
    if t < close_time and d in closes.index:      # 場中開示: 前日終値 → 当日終値
        i_ref, i_out = bisect_left(days, d) - 1, bisect_left(days, d)
    else:                                         # 引け後・休日: 当日(以前)終値 → 翌営業日終値
        i_ref, i_out = bisect_right(days, d) - 1, bisect_right(days, d)
    if i_ref < 0 or i_out >= len(days):
        return None
    ref, out = float(closes.iloc[i_ref]), float(closes.iloc[i_out])
    if ref <= 0:
        return None
    return days[i_ref].isoformat(), days[i_out].isoformat(), round((out / ref - 1) * 100, 2)


def compute_reactions(disc: pd.DataFrame, closes: dict[str, pd.Series], close_time: str,
                      max_abs_pct: float) -> pd.DataFrame:
    rows = []
    for r in disc.itertuples():
        s = closes.get(r.code)
        if s is None or s.empty:
            continue
        x = reaction_of(r.disclosed_at, s, close_time)
        if x and abs(x[2]) <= max_abs_pct:          # 分割未調整などの異常値は除外
            rows.append({"disclosure_id": r.disclosure_id, "code": r.code,
                         "ref_date": x[0], "out_date": x[1], "ret_pct": x[2]})
    return pd.DataFrame(rows, columns=REACTION_COLUMNS)


def build_stats(disc: pd.DataFrame, reactions: pd.DataFrame, large_pct: float) -> dict:
    df = disc.merge(reactions, on=["disclosure_id", "code"], how="inner")
    df["bucket"] = [bucket(r, large_pct) for r in df.to_dict("records")]
    df = df.dropna(subset=["bucket"])
    if df.empty:
        return {"base_rate": 0.5, "n": 0, "buckets": {}}
    up = df["ret_pct"] > 0
    out = {"base_rate": round(float(up.mean()), 4), "n": int(len(df)), "buckets": {}}
    for b, g in df.groupby("bucket"):
        out["buckets"][b] = {"n": int(len(g)), "wins": int((g["ret_pct"] > 0).sum()),
                             "mean_ret_pct": round(float(g["ret_pct"].mean()), 2)}
    return out


def probability(b: str | None, stats: dict, prior_n: float) -> float | None:
    """縮小推定した上昇確度(0〜1)。"""
    if b is None or not stats.get("n"):
        return None
    base = stats["base_rate"]
    s = stats["buckets"].get(b, {"n": 0, "wins": 0})
    return (s["wins"] + prior_n * base) / (s["n"] + prior_n)


# ---------------------------------------------------------------- 入出力

def load_stats(data_dir: Path = DATA_DIR) -> dict:
    p = stats_path(data_dir)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def update(cfg: dict, data_dir: Path = DATA_DIR) -> dict:
    rcfg = cfg["reaction"]
    disc = disclosures.load(data_dir)
    disc = disc[[bucket(r, rcfg["large_change_pct"]) is not None for r in disc.to_dict("records")]]
    closes = yf.daily_closes(sorted(disc["code"].unique()), period=rcfg["price_period"])
    reactions = compute_reactions(disc, closes, rcfg["close_time"], rcfg["max_abs_ret_pct"])

    # 過去分は消えないよう既存と統合(TDnetは約1ヶ月、yfinanceの期間も有限なため)
    p = reactions_path(data_dir)
    if p.exists():
        old = pd.read_csv(p, dtype={"disclosure_id": str, "code": str})
        reactions = pd.concat([old[~old["disclosure_id"].isin(reactions["disclosure_id"])], reactions])
    reactions = reactions.sort_values(["out_date", "disclosure_id"]).reset_index(drop=True)
    reactions.to_csv(p, index=False, encoding="utf-8")

    stats = build_stats(disclosures.load(data_dir), reactions, rcfg["large_change_pct"])
    stats_path(data_dir).write_text(json.dumps(stats, ensure_ascii=False, indent=2, sort_keys=True),
                                    encoding="utf-8")
    log.info("reaction: n=%d base=%.3f buckets=%s", stats["n"], stats["base_rate"],
             {k: v["n"] for k, v in stats["buckets"].items()})
    return {"reactions": len(reactions), "n": stats["n"], "base_rate": stats["base_rate"]}
