"""各社の過去の決算短信の発表時刻 → data/announce_times.csv(決算予定ページの「発表時刻の目安」)

JPX の決算発表予定日には時刻が無い。ほとんどの会社は毎回同じ時刻に出すので、前回の短信の時刻を目安にする。
過去の時刻は手元の IRBANK 開示索引(~/kessan-pdfs/index、手元のみ)から週1回作り直し、
直近の四半期は TDnet 監視の disclosures.csv(リポジトリ内・毎分更新)を描画時に重ねる。
"""
import logging
from collections import Counter
from datetime import timedelta
from pathlib import Path

import pandas as pd

from . import calendar_fetch, disclosures
from .config import DATA_DIR, now_jst
from .datasources import irbank_api

log = logging.getLogger(__name__)

COLUMNS = ["code", "announce_date", "announce_time"]     # 1行 = その会社の過去の短信1回(同じ日は最初の1本)
KEEP = 8                                                # 会社ごとに直近8回
REFRESH_DAYS = 7
REFRESH_PER_RUN = 60                                    # 1回(10分おき)に API で取り直す社数の上限
# 短信本体だけ(訂正・期中レビュー完了の出し直しは除く)
EXCLUDE = r"訂正|レビューの完了|レビュー完了|補足|説明資料"


def path(data_dir: Path = DATA_DIR) -> Path:
    return data_dir / "announce_times.csv"


def load(data_dir: Path = DATA_DIR) -> pd.DataFrame:
    p = path(data_dir)
    if not p.exists():
        return pd.DataFrame(columns=COLUMNS)
    return pd.read_csv(p, dtype=str, keep_default_na=False).reindex(columns=COLUMNS, fill_value="")


def _reports(df: pd.DataFrame, title: str = "title") -> pd.DataFrame:
    return df[df[title].str.contains("決算短信", na=False) & ~df[title].str.contains(EXCLUDE, na=False)]


def from_index(idx: pd.DataFrame) -> pd.DataFrame:
    """IRBANK 索引(disclosed_at は UTC)→ code, announce_date, announce_time(JST)"""
    er = _reports(idx[idx["source"] == "tdnet"])
    t = pd.to_datetime(er["disclosed_at"], utc=True, errors="coerce").dt.tz_convert("Asia/Tokyo")
    return pd.DataFrame({"code": er["code"], "announce_date": t.dt.strftime("%Y-%m-%d"),
                         "announce_time": t.dt.strftime("%H:%M")}).dropna()


def from_disclosures(disc: pd.DataFrame) -> pd.DataFrame:
    """TDnet 監視(disclosed_at は JST)→ 同じ形"""
    er = disc[(disc["kind"] == "earnings_report") & (disc["is_correction"].astype(str) != "True")]
    return pd.DataFrame({"code": er["code"], "announce_date": er["disclosed_at"].str[:10],
                         "announce_time": er["disclosed_at"].str[11:16]})


def combine(*frames: pd.DataFrame) -> pd.DataFrame:
    """同じ会社・同じ日は最初の時刻だけ。会社ごとに新しい順に KEEP 回"""
    d = pd.concat(frames, ignore_index=True).reindex(columns=COLUMNS)
    d = d[d["code"].astype(str).str.fullmatch(r"[0-9A-Z]{4}") & d["announce_time"].str.fullmatch(r"\d\d:\d\d")]
    d = d.sort_values(["code", "announce_date", "announce_time"]).drop_duplicates(["code", "announce_date"])
    d = d.sort_values(["code", "announce_date"], ascending=[True, False]).groupby("code").head(KEEP)
    return d.reset_index(drop=True)


def estimate(hist: list[tuple[str, str]]) -> dict | None:
    """[(日付, 時刻)] 新しい順 → 目安(前回の時刻)と過去の内訳"""
    if not hist:
        return None
    times = [t for _, t in hist]
    cnt = Counter(times)
    return {"time": times[0], "last_date": hist[0][0], "n": len(times), "same": cnt[times[0]],
            "others": [f"{t}×{c}" if c > 1 else t for t, c in cnt.most_common() if t != times[0]]}


def estimates(data_dir: Path = DATA_DIR, disc: pd.DataFrame | None = None) -> dict[str, dict]:
    """描画用: 週1回の索引分 + TDnet 監視分 → {code: 目安}"""
    disc = disclosures.load(data_dir) if disc is None else disc
    d = combine(load(data_dir), from_disclosures(disc))
    return {c: estimate(list(zip(g["announce_date"], g["announce_time"]))) for c, g in d.groupby("code")}


def update(cfg: dict, data_dir: Path = DATA_DIR, fetch: bool = True) -> dict:
    """手元のみ。決算予定の銘柄のうち索引が REFRESH_DAYS 日より古いものを API で取り直し(1回 REFRESH_PER_RUN 社まで)、
    索引から announce_times.csv を作り直す。変わった行数を返す"""
    codes = sorted(set(calendar_fetch.load(data_dir)["code"]))
    week_ago = (now_jst() - timedelta(days=REFRESH_DAYS)).strftime("%Y-%m-%dT%H:%M")
    refreshed = 0
    if fetch:
        idx = irbank_api.load_index(data_dir)
        last = idx.groupby("code")["fetched_at"].max().to_dict() if not idx.empty else {}
        for c in [c for c in codes if last.get(c, "") < week_ago][:REFRESH_PER_RUN]:
            irbank_api.earnings_reports(c, data_dir, fetch=True, fresh_after=week_ago)
            refreshed += 1
    idx, old = irbank_api.load_index(data_dir), load(data_dir)
    new = combine(old, from_index(idx[idx["code"].isin(codes)])) if not idx.empty else old
    changed = len(set(new.itertuples(index=False, name=None)) ^ set(old.itertuples(index=False, name=None)))
    if changed:
        data_dir.mkdir(parents=True, exist_ok=True)
        new.to_csv(path(data_dir), index=False, encoding="utf-8")
    stats = {"codes": len(codes), "refreshed": refreshed,
             "with_history": int(new.loc[new["code"].isin(codes), "code"].nunique()), "changed_rows": changed}
    log.info("announce_times: %s", stats)
    return stats
