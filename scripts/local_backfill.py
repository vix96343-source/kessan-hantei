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

from src import orders, segments, site  # noqa: E402
from src.config import DATA_DIR, load_config  # noqa: E402

LOG = ROOT / "data" / "cache" / "local_backfill.log"
log = logging.getLogger("local_backfill")
FILES = {"segments": segments, "orders": orders}


def git(*args, check=True):
    return subprocess.run(["git", *args], cwd=ROOT, check=check, capture_output=True, text=True, encoding="utf-8")


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
    rows = added_rows(before, snapshot())
    log.info("backfill %s / 追加 %s", stats, {k: len(v) for k, v in rows.items()})
    if not rows:
        return 0
    for attempt in range(5):
        if attempt:
            git("fetch", "-q", "origin")
            git("reset", "-q", "--hard", "origin/main")
            apply_rows(rows)
        site.render_all(cfg)
        git("add", "data", "docs")
        git("commit", "-qm", "data: 前の四半期のセグメント・受注を補完(手元・IRBANK)", check=False)
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
