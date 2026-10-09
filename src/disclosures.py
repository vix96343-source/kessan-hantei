"""適時開示(修正開示・決算短信・説明資料)の取り込みと revision_recency 因子の計算。

disclosures.csv は disclosure_id をキーに追記・更新する(TDnetは約1ヶ月で消えるため
洗い替えはしない)。disclosures_meta.json の coverage_start 以降が欠損なしの期間。
"""
import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from .config import DATA_DIR, now_jst
from .datasources import tdnet
from .datasources.http import RateLimitedSession

log = logging.getLogger(__name__)

COLUMNS = [
    "disclosure_id", "disclosed_at", "code", "name", "title", "kind", "is_correction",
    "direction", "metric", "change_pct", "period", "xbrl_changes",
    "pdf_url", "xbrl_url", "exchange", "fetched_at",
]


def csv_path(data_dir: Path = DATA_DIR) -> Path:
    return data_dir / "disclosures.csv"


def meta_path(data_dir: Path = DATA_DIR) -> Path:
    return data_dir / "disclosures_meta.json"


def load(data_dir: Path = DATA_DIR) -> pd.DataFrame:
    p = csv_path(data_dir)
    if not p.exists():
        return pd.DataFrame(columns=COLUMNS)
    df = pd.read_csv(p, dtype=str, keep_default_na=False)
    return df.reindex(columns=COLUMNS, fill_value="")


def load_meta(data_dir: Path = DATA_DIR) -> dict:
    p = meta_path(data_dir)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def _universe_codes(data_dir: Path) -> set[str] | None:
    p = data_dir / "universe.csv"
    if not p.exists():
        return None
    return set(pd.read_csv(p, dtype=str)["code"].str.strip())


def _save(df: pd.DataFrame, data_dir: Path) -> None:
    df = df.sort_values(["disclosed_at", "disclosure_id"], ascending=False)
    tmp = csv_path(data_dir).with_suffix(".tmp")
    df.to_csv(tmp, index=False, encoding="utf-8")
    tmp.replace(csv_path(data_dir))


def ingest(cfg: dict, days: int | None = None, data_dir: Path = DATA_DIR,
           session: RateLimitedSession | None = None, today: date | None = None) -> dict:
    """TDnetから当日〜days日前までの開示を取り込む。"""
    dcfg = cfg["disclosures"]
    session = session or RateLimitedSession.from_config(cfg)
    today = today or now_jst().date()
    meta = load_meta(data_dir)
    days = dcfg["lookback_days"] if days is None else days
    # 前回取得日から空白があれば自動で埋める(Actionsが止まっていた場合の取りこぼし防止)
    if meta.get("last_date"):
        gap = (today - date.fromisoformat(meta["last_date"])).days
        days = max(days, gap)
        if gap > dcfg["backfill_max_days"]:
            log.warning("前回取得から%d日空いたため履歴が途切れます(coverage_start をリセット)", gap)
            meta.pop("coverage_start", None)
    days = min(days, dcfg["backfill_max_days"])
    store_kinds = set(dcfg["store_kinds"])
    xbrl_kinds = set(dcfg["parse_xbrl_kinds"])
    universe = _universe_codes(data_dir) if dcfg.get("filter_by_universe") else None
    if dcfg.get("filter_by_universe") and universe is None:
        log.info("universe.csv が無いため全銘柄を保存します")

    df = load(data_dir)
    rows = {r["disclosure_id"]: r for r in df.to_dict("records")}
    fetched_at = now_jst().strftime("%Y-%m-%dT%H:%M")
    fetched_dates = []
    n_new = n_xbrl = 0

    for offset in range(days, -1, -1):
        d = today - timedelta(days=offset)
        items = tdnet.fetch_day(session, d)
        if items is None:
            log.info("%s: 一覧なし(公開期間外)", d)
            continue
        fetched_dates.append(d)
        for it in items:
            c = tdnet.classify_title(it.title)
            if c["kind"] not in store_kinds:
                continue
            if universe is not None and it.code not in universe:
                continue
            row = rows.get(it.disclosure_id)
            if row is None:
                row = {**it.to_dict(), "kind": c["kind"], "is_correction": str(c["is_correction"]),
                       "direction": c["title_direction"], "metric": "", "change_pct": "",
                       "period": "", "xbrl_changes": "", "fetched_at": fetched_at}
                rows[it.disclosure_id] = row
                n_new += 1
            elif it.xbrl_url and not row.get("xbrl_url"):
                row["xbrl_url"] = it.xbrl_url

            # XBRL未解析の修正開示だけ取りに行く(冪等)
            if row["kind"] in xbrl_kinds and row.get("xbrl_url") and not row.get("period"):
                try:
                    x = tdnet.fetch_forecast_revision(session, row["xbrl_url"])
                except Exception as e:  # 1件の失敗で全体を止めない。次回再試行
                    log.warning("XBRL取得失敗 %s: %s", row["xbrl_url"], e)
                    continue
                n_xbrl += 1
                row["period"] = x.get("period", "-")   # "-" = 解析済みだが読めなかった
                if "change_pct" in x:
                    row["metric"] = x["metric"]
                    row["change_pct"] = str(x["change_pct"])
                    if row["direction"] in ("", "mixed"):
                        row["direction"] = x["direction"]
                row["xbrl_changes"] = tdnet.changes_json(x.get("changes", {}))

    out = pd.DataFrame(list(rows.values())).reindex(columns=COLUMNS, fill_value="") if rows \
        else pd.DataFrame(columns=COLUMNS)
    data_dir.mkdir(parents=True, exist_ok=True)
    _save(out, data_dir)

    # 実行時刻は書かない(日付が変わらなければファイルも変わらず、無駄なコミットを作らない)
    if fetched_dates:
        start = min(fetched_dates).isoformat()
        if not meta.get("coverage_start") or start < meta["coverage_start"]:
            meta["coverage_start"] = start
        meta["last_date"] = max(today.isoformat(), meta.get("last_date", ""))
        meta_path(data_dir).write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    stats = {"days_fetched": len(fetched_dates), "new_rows": n_new, "xbrl_parsed": n_xbrl, "total_rows": len(out)}
    log.info("disclosures: %s", stats)
    return stats


# ---------------------------------------------------------------- 因子

def revision_recency(code: str, asof: date, df: pd.DataFrame, fcfg: dict,
                     coverage_start: str | None = None) -> dict:
    """直近 window_days 以内の業績予想修正からペナルティ値(0〜1)を返す。

    value は judge 側で負の重み(config: factors.revision_recency.weight)を掛けて使う。
    coverage_ok=False のときは履歴が窓をカバーしていない(修正の見落としがありうる)。
    """
    window = fcfg["window_days"]
    since = asof - timedelta(days=window)
    d = df[df["code"] == code].copy()
    d["date"] = d["disclosed_at"].str[:10]

    earnings_days = set(d.loc[d["kind"] == "earnings_report", "date"])
    rev = d[d["kind"].isin(fcfg["count_kinds"])]
    if fcfg.get("exclude_corrections", True):
        rev = rev[rev["is_correction"] != "True"]
    if fcfg.get("exclude_same_day_as_earnings", True):
        rev = rev[~rev["date"].isin(earnings_days)]
    rev = rev[(rev["date"] > since.isoformat()) & (rev["date"] <= asof.isoformat())]

    coverage_ok = bool(coverage_start) and coverage_start <= since.isoformat()
    if rev.empty:
        return {"value": 0.0, "n_revisions": 0, "last_date": "", "last_direction": "",
                "last_change_pct": None, "days_since": None, "coverage_ok": coverage_ok}

    last = rev.sort_values("disclosed_at").iloc[-1]
    days_since = (asof - datetime.strptime(last["date"], "%Y-%m-%d").date()).days
    value = 1.0 if fcfg.get("decay") == "flat" else round(1 - days_since / window, 4)
    return {
        "value": value,
        "n_revisions": len(rev),
        "last_date": last["date"],
        "last_direction": last["direction"],
        "last_change_pct": float(last["change_pct"]) if last["change_pct"] else None,
        "days_since": days_since,
        "coverage_ok": coverage_ok,
    }
