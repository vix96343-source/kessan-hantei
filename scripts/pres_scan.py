"""手元の決算説明資料(~/kessan-pdfs)のうち、各社の一番新しいものの文字を読んで(.txt に保存)、
受注・セグメントの数値の表らしい行がどれだけあるかを数える(読み取りを作る前の下調べ)"""
import json
import logging
import re
import sys
from multiprocessing import Pool
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
logging.disable(logging.CRITICAL)

from src.datasources import irbank_api, tdnet  # noqa: E402

PDF = Path.home() / "kessan-pdfs"
NUM = r"[△▲\-]?[\d,]+(?:\.\d+)?%?"
ROW = re.compile(r"^(?P<lab>[^\d△▲\-]{1,30}?)\s+(?P<nums>(?:" + NUM + r"\s+){3,}" + NUM + r")\s*$")
QHDR = re.compile(r"(1Q|2Q|3Q|4Q|Q1|Q2|Q3|Q4)\s+(1Q|2Q|3Q|4Q|Q1|Q2|Q3|Q4)")


def text_of(code: str, doc: str) -> str:
    """説明資料の文字(pdfium で読む。速い)。.txt に保存して使い回す"""
    import pypdfium2 as pdfium
    pdf, txt = PDF / code / f"{doc}.pdf", PDF / code / f"{doc}.txt"
    if txt.exists():
        return txt.read_text(encoding="utf-8")
    try:
        d = pdfium.PdfDocument(str(pdf))
        t = "\n".join(d[i].get_textpage().get_text_range() for i in range(len(d)))
        t = t.replace("\r\n", "\n").replace("\r", "\n")
    except Exception:
        t = ""
    txt.write_text(t, encoding="utf-8")
    return t


def scan_one(a):
    code, doc = a
    if not (PDF / code / f"{doc}.pdf").exists():
        return None
    t = text_of(code, doc)
    lines = [l.strip() for l in t.splitlines() if l.strip()]
    orows = [l for l in lines if re.search(r"受注(高|額|残)", l) and ROW.match(l)]
    return code, len(orows), bool(QHDR.search(t))


if __name__ == "__main__":
    idx = irbank_api.load_index()
    idx = idx[(idx.doc_id != "") & idx.title.map(lambda t: tdnet.classify_title(t)["kind"] == "earnings_presentation")]
    latest = idx.sort_values("filing_date").groupby("code").tail(1)
    out = {"companies": 0, "order_rows": 0, "order_cos": [], "quarter_hdr_cos": 0}
    with Pool(12) as pool:
        jobs = [(r.code, r.doc_id) for r in latest.itertuples()]
        for k, res in enumerate(pool.imap_unordered(scan_one, jobs, chunksize=4)):
            if res:
                code, n, qh = res
                out["companies"] += 1
                if n:
                    out["order_rows"] += n
                    out["order_cos"].append(code)
                out["quarter_hdr_cos"] += qh
            if k % 300 == 0:
                print(k, len(jobs), {a: (len(b) if isinstance(b, list) else b) for a, b in out.items()}, flush=True)
    (PDF / "pres_scan.json").write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    print("end", {a: (len(b) if isinstance(b, list) else b) for a, b in out.items()})
