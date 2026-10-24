"""セグメント・受注の過去の四半期を、手元の過去の短信 PDF(~/kessan-pdfs)から埋めて push する(手元でのみ)。

1. 読めなかった(none の)補完の行を消して読み直させる
2. 前の四半期(セグメント・受注)と、約2年分の推移(セグメント・受注)を補う
3. 補った行だけを最新の data/ に足し、サイトを作り直して push(ぶつかったら取り直して足し直す)
"""
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import local_backfill as lb  # noqa: E402
from src import orders, segments, site  # noqa: E402
from src.config import DATA_DIR, load_config  # noqa: E402

log = logging.getLogger("history_fill")


def main() -> int:
    cfg = load_config()
    lb.git("fetch", "-q", "origin")
    lb.git("checkout", "-q", "--", "data", "docs")
    lb.git("reset", "-q", "--hard", "origin/main")
    before = lb.snapshot()
    for m in (segments, orders):                       # IRBANK から補って読めなかった行(T00:00)を読み直させる
        df = m.load(DATA_DIR)
        drop = (df["status"] != "ok") & df["disclosed_at"].str.endswith("T00:00")
        df[~drop].to_csv(m.path(DATA_DIR), index=False, encoding="utf-8")
        print(m.__name__, "読み直し", int(drop.sum()), flush=True)
    print("セグメント 前の四半期", segments.backfill(cfg), flush=True)
    print("セグメント 推移", segments.backfill_history(cfg, depth=11), flush=True)
    print("受注 前の四半期", orders.backfill(cfg), flush=True)
    print("受注 推移", orders.backfill_history(cfg, depth=11), flush=True)
    rows = lb.added_rows(before, lb.snapshot())
    rows = {k: v[v["status"] == "ok"] for k, v in rows.items()}
    print("足す行", {k: len(v) for k, v in rows.items()}, flush=True)
    if not any(len(v) for v in rows.values()):
        return 0
    for _ in range(5):
        lb.git("fetch", "-q", "origin")
        lb.git("checkout", "-q", "--", "data", "docs")
        lb.git("reset", "-q", "--hard", "origin/main")
        lb.apply_rows(rows)
        site.render_all(cfg)
        lb.git("add", "data", "docs")
        lb.git("commit", "-qm", "data: 過去の短信 PDF からセグメント・受注の推移を補完\n\n"
               "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>", check=False)
        if lb.git("push", "-q", "origin", "HEAD:main", check=False).returncode == 0:
            print("pushed", flush=True)
            return 0
    print("push できなかった", flush=True)
    return 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    lb.ROOT = ROOT
    sys.exit(main())
