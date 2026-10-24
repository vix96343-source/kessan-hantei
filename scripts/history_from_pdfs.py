"""手元の過去の決算短信 PDF(~/kessan-pdfs、約2年分)の1ページ目から、全上場企業の単独四半期の業績
(売上・営業利益・経常利益・純利益)を作り、data/quarterly_history.csv に足りない四半期だけ足す(手元でのみ)。

- 1ページ目の「経営成績」は期首からの累計と前年同期の累計。3か月の値 = その期の累計 − 前の四半期までの累計
  (1Q はそのまま)。前年同期の累計からも同じ引き算で前年の四半期を作る。
- 既にある四半期(IRBANK・TDnet の XBRL から作ったもの)は上書きしない(source=pdf で足す)。
"""
import logging
import sys
from collections import defaultdict
from multiprocessing import Pool
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
logging.disable(logging.CRITICAL)

from src import history  # noqa: E402
from src.datasources import irbank_api, tdnet  # noqa: E402

PDF = Path.home() / "kessan-pdfs"
FIELDS = history.FIELDS


def ym_shift(ym: str, months: int) -> str:
    y, m = int(ym[:4]), int(ym[5:7]) + months
    y, m = y + (m - 1) // 12, (m - 1) % 12 + 1
    return f"{y:04d}-{m:02d}"


def read_one(a):
    code, doc, title, filed = a
    pdf = PDF / code / f"{doc}.pdf"
    if not pdf.exists():
        return None
    try:
        x = tdnet.parse_summary_pdf(pdf.read_bytes(), title)
    except Exception:
        return None
    if not x or not x.get("period_end"):
        return None
    return code, filed, x["n_q"], x["period_end"][:7], x.get("cum") or {}, x.get("prior_cum") or {}


def main() -> int:
    idx = irbank_api.load_index()
    idx = idx[(idx["doc_id"] != "") & (idx["source"] == "tdnet")]
    cls = idx["title"].map(tdnet.classify_title)
    reps = idx[cls.map(lambda c: c["kind"] == "earnings_report" and not c["is_correction"])]
    jobs = [(r.code, r.doc_id, r.title, r.filing_date) for r in reps.itertuples()]
    print("短信", len(jobs), flush=True)
    cum = defaultdict(dict)                            # code -> ym -> (n_q, {field: 円}, 発表日)
    with Pool(12) as pool:
        for k, res in enumerate(pool.imap_unordered(read_one, jobs, chunksize=16)):
            if res:
                code, filed, n_q, ym, cur, prior = res
                if any(cur.get(f) is not None for f in FIELDS):
                    cum[code][ym] = (n_q, cur, filed)
                py = ym_shift(ym, -12)                 # 前年同期の累計(新しい短信の値を優先しない: 既にあれば残す)
                if any(prior.get(f) is not None for f in FIELDS) and py not in cum[code]:
                    cum[code][py] = (n_q, prior, "")
            if k % 2000 == 0:
                print(k, len(jobs), flush=True)
    hist = history.load()
    have = set(hist["code"] + "|" + hist["end"])
    rows = []
    for code, by in cum.items():
        for ym, (n_q, c, filed) in by.items():
            if f"{code}|{ym}" in have:
                continue
            if filed and filed[:7] <= ym:
                continue                               # 期末より前の発表は決算期の変更(変則決算)の読み違い
            if n_q == 1:
                single = dict(c)
            else:
                prev = by.get(ym_shift(ym, -3))
                if not prev or prev[0] != n_q - 1:
                    continue
                single = {f: (None if c.get(f) is None or prev[1].get(f) is None else c[f] - prev[1][f]) for f in FIELDS}
            if single.get("sales") is not None and single["sales"] < 0:
                continue                               # 売上の3か月がマイナスは読み違い
            if all(single.get(f) is None for f in FIELDS):
                continue
            rows.append({"code": code, "end": ym, **{f: single.get(f) for f in FIELDS}, "announced": filed, "source": "pdf"})
    print("足す四半期", len(rows), "会社", len({r["code"] for r in rows}), flush=True)
    history.save(history.merge(hist, rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
