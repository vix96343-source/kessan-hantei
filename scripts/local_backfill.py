"""手元の PC で定期的に動かす補完(Windows のタスク スケジューラから10分ごと)。

IRBANK は GitHub Actions からつながらないので、セグメント別・受注の表で「累」(前の四半期の記録が無い)に
ならないよう、前の四半期の決算短信 PDF をここで読んで data/ に足し、サイトを作り直して push する。
足す行が無ければ何もしない。Actions の watch も同じブランチに push するので、ぶつかったら取り直して足し直す。
"""
import logging
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from src import announce_times, forecasts, ir_times, irdocs, judge, orders, segments, site  # noqa: E402
from src.config import DATA_DIR, load_config  # noqa: E402

LOG = ROOT / "data" / "cache" / "local_backfill.log"
log = logging.getLogger("local_backfill")
FILES = {"segments": segments, "orders": orders, "forecasts": forecasts}


def git(*args, check=True):
    # タスク スケジューラから pythonw で動かすとき、git の黒い窓を出さない
    return subprocess.run(["git", *args], cwd=ROOT, check=check, capture_output=True, text=True, encoding="utf-8",
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def snapshot() -> dict[str, pd.DataFrame]:
    return {k: m.load(DATA_DIR) for k, m in FILES.items()}


def added_rows(before: dict, after: dict) -> dict[str, pd.DataFrame]:
    """補完で増えた・変わった行(disclosure_id ごと)"""
    out = {}
    for k in FILES:
        old = set(before[k].astype(str).itertuples(index=False, name=None))
        a = after[k]
        new = a[[t not in old for t in a.astype(str).itertuples(index=False, name=None)]]
        if len(new):
            out[k] = new
    return out


def apply_rows(rows: dict[str, pd.DataFrame]) -> None:
    """取り直した最新の data/ に補完の行を足す(同じ disclosure_id は補完の行で置き換え)"""
    for k, new in rows.items():
        m = FILES[k]
        df = m.load(DATA_DIR)
        out = pd.concat([df[~df["disclosure_id"].isin(new["disclosure_id"])], new.reindex(columns=m.COLUMNS)])
        out.sort_values(["disclosed_at", "disclosure_id"], ascending=False).to_csv(m.path(DATA_DIR), index=False,
                                                                                 encoding="utf-8")


def main() -> int:
    cfg = load_config()
    git("fetch", "-q", "origin")
    git("reset", "-q", "--hard", "origin/main")
    before = snapshot()
    stats = {"segments": segments.backfill(cfg), "orders": orders.backfill(cfg)}
    # 判定(judge)用: 決算予定の銘柄の前回の短信(会社予想)。Actions の daily(20:05〜)の判定より前に足しておく
    try:
        t = judge.backfill_targets(cfg)
        stats["forecasts"] = forecasts.backfill(cfg, t, judge.have_recent_snapshot(t))
    except Exception as e:
        log.warning("forecasts backfill 失敗: %s", e)
    # 決算予定ページの時刻の目安: 索引が1週間より古い銘柄だけ取り直し、変わったときだけ書き直す
    try:
        stats["announce_times"] = announce_times.update(cfg)
    except Exception as e:
        log.warning("announce_times 失敗: %s", e)
    times = announce_times.load(DATA_DIR) if stats.get("announce_times", {}).get("changed_rows") else None
    # 決算予定の時刻: 各社 IR カレンダーの予定時刻(未発表の銘柄を1日1回ずつ、1回6分まで。タスクからはブラウザが起動できない)
    urls_before = irdocs._load_urls(DATA_DIR)
    try:
        stats["ir_times"] = ir_times.update(cfg, time_budget_sec=360, render=False)   # 静的なページだけで十分(17社中16社)
    except Exception as e:
        log.warning("ir_times 失敗: %s", e)
    irt = ir_times.load(DATA_DIR) if stats.get("ir_times", {}).get("checked") else None
    new_urls = {k: v for k, v in irdocs._load_urls(DATA_DIR).items() if urls_before.get(k) != v}
    rows = added_rows(before, snapshot())
    log.info("backfill %s / 追加 %s", stats, {k: len(v) for k, v in rows.items()})
    if not rows and times is None and irt is None:
        return 0
    for attempt in range(5):
        if attempt:
            git("fetch", "-q", "origin")
            git("reset", "-q", "--hard", "origin/main")
            apply_rows(rows)
            if times is not None:
                times.to_csv(announce_times.path(DATA_DIR), index=False, encoding="utf-8")
            if irt is not None:
                irt.to_csv(ir_times.path(DATA_DIR), index=False, encoding="utf-8")
            if new_urls:                                   # 会社URLは Actions も足すので、取り直した分に足す
                irdocs._save_urls({**irdocs._load_urls(DATA_DIR), **new_urls}, DATA_DIR)
        site.render_all(cfg)
        git("add", "data", "docs")
        git("commit", "-qm", "data: 前の四半期のセグメント・受注・会社予想・発表時刻(IRBANK・各社IR)を補完(手元)", check=False)
        if git("push", "-q", "origin", "HEAD:main", check=False).returncode == 0:
            log.info("push ok")
            return 0
        log.info("push がぶつかったので取り直す (%d)", attempt + 1)
    log.warning("push できなかった")
    return 1


if __name__ == "__main__":
    LOG.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s",
                        handlers=[logging.FileHandler(LOG, encoding="utf-8")])
    try:
        sys.exit(main())
    except Exception:
        log.exception("失敗")
        sys.exit(1)
