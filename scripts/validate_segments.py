"""過去の短信 PDF から読んだセグメントの記録(src=irbank_pdf)を IRBANK のセグメント別業績と照らし(segments.irbank_consistent)、
合わない記録は使わない(status=bad。補完で読み直さない)。最新の data/ に当てて push する(手元でのみ)。"""
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
logging.disable(logging.CRITICAL)

import local_backfill as lb  # noqa: E402
from src import segments, site  # noqa: E402
from src.config import load_config  # noqa: E402
from src.datasources import irbank_api  # noqa: E402


def find_bad() -> list[str]:
    df = segments.load()
    ok = df[(df["status"] == "ok") & df["data"].str.contains('"src": "irbank_pdf"')]
    bad = []
    codes = sorted(set(ok["code"]))
    for i, code in enumerate(codes):
        if irbank_api.segment_cumulative(code) is None:
            print("IRBANK に聞けない(上限など)。ここまで", flush=True)
            break
        for r in ok[ok["code"] == code].to_dict("records"):
            if not segments.irbank_consistent(code, str(r["period_end"])[:7], json.loads(r["data"])):
                bad.append(r["disclosure_id"])
        if i % 50 == 0:
            print(i, len(codes), "合わない記録", len(bad), flush=True)
    return bad


def main() -> int:
    lb.ROOT = ROOT
    cfg = load_config()
    lb.git("fetch", "-q", "origin")
    lb.git("checkout", "-q", "--", "data", "docs")
    lb.git("reset", "-q", "--hard", "origin/main")
    bad = find_bad()
    print("使わない記録", len(bad), flush=True)
    for _ in range(5):
        lb.git("fetch", "-q", "origin")
        lb.git("checkout", "-q", "--", "data", "docs")
        lb.git("reset", "-q", "--hard", "origin/main")
        df = segments.load()
        m = df["disclosure_id"].isin(bad)
        df.loc[m, "status"], df.loc[m, "data"] = "bad", ""
        df.to_csv(segments.path(), index=False, encoding="utf-8")
        site.render_all(cfg)
        lb.git("add", "data", "docs")
        lb.git("commit", "-qm", "data: IRBANK と合わない過去のセグメントの記録を使わない\n\n"
               "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>", check=False)
        if lb.git("push", "-q", "origin", "HEAD:main", check=False).returncode == 0:
            print("pushed", flush=True)
            return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
