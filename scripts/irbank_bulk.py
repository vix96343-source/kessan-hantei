"""IRBANK Connect の API で、上場企業の過去約2年分の決算短信・決算説明資料を手元に集める(続きから再開できる)。

1. 上場銘柄の一覧(ETF・REIT などを除く)
2. 銘柄ごとの開示一覧(2024-07-01 以降)を ~/kessan-pdfs/irbank_index.csv に(1 日 3,900 回まで。超えたら翌日に続き)
3. 決算短信(訂正を除く)と決算説明資料の PDF を ~/kessan-pdfs/{コード}/{書類番号}.pdf に(認証不要・1 秒に 1 件)

Windows のタスク スケジューラから 1 時間ごとに動かす(終わっていれば何もしない)。ログは ~/kessan-pdfs/irbank_bulk.log。
"""
import json
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import requests  # noqa: E402

from src.config import DATA_DIR  # noqa: E402
from src.datasources import irbank_api, tdnet  # noqa: E402

SINCE = "2024-07-01"                                   # 2年分 + 前年同期比のための1四半期
PDF_DIR = Path.home() / "kessan-pdfs"
SEC_CACHE = PDF_DIR / "irbank_securities.json"
LOG = PDF_DIR / "irbank_bulk.log"
log = logging.getLogger("irbank_bulk")


def wanted(title: str) -> bool:
    """決算短信(訂正を除く)と決算説明資料"""
    c = tdnet.classify_title(title or "")
    return (c["kind"] in ("earnings_report", "earnings_presentation")) and not c["is_correction"]


def securities(client: irbank_api.Client) -> list[str]:
    if SEC_CACHE.exists():
        secs = json.loads(SEC_CACHE.read_text(encoding="utf-8"))
    else:
        secs = client.securities()
        SEC_CACHE.write_text(json.dumps(secs, ensure_ascii=False), encoding="utf-8")
    return [s["code"] for s in secs if s.get("market") and s.get("industry") not in ("ETF", "ETN", "REIT", "インフラファンド")]


def build_index(client: irbank_api.Client) -> bool:
    """開示一覧を集める。全部終われば True"""
    codes = securities(client)
    done = set(irbank_api.load_index()["code"])
    todo = [c for c in codes if c not in done]
    log.info("索引: 全 %d 銘柄、残り %d", len(codes), len(todo))
    for i, code in enumerate(todo):
        irbank_api.append_index(code, client.disclosures(code, SINCE))
        if i % 50 == 0:
            log.info("索引 %d / %d (%s)", i, len(todo), code)
    return True


def download_pdfs() -> int:
    idx = irbank_api.load_index()
    idx = idx[(idx["doc_id"] != "") & (idx["source"] == "tdnet") & (idx["filing_date"] >= SINCE)]
    s = requests.Session()
    s.headers["User-Agent"] = "kessan-hantei"
    n = 0
    for r in idx.to_dict("records"):
        if not wanted(r["title"]):
            continue
        out = PDF_DIR / r["code"] / f"{r['doc_id']}.pdf"
        if out.exists() and out.stat().st_size > 0:
            continue
        time.sleep(1.0)
        try:
            resp = s.get(r["pdf_url"], timeout=60)
        except requests.RequestException as e:
            log.warning("PDF 失敗 %s %s", r["doc_id"], e)
            continue
        if resp.status_code != 200 or not resp.content.startswith(b"%PDF"):
            log.warning("PDF なし %s %s", r["doc_id"], resp.status_code)
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(resp.content)
        n += 1
        if n % 200 == 0:
            log.info("PDF %d 件", n)
    return n


def main() -> int:
    client = irbank_api.Client()
    try:
        build_index(client)
    except irbank_api.QuotaExceeded as e:
        log.info("索引: %s。続きは明日", e)
    n = download_pdfs()                                # 索引ができた分の PDF は今日のうちに取る
    log.info("PDF 取得 %d 件", n)
    return 0


if __name__ == "__main__":
    LOG.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s",
                        handlers=[logging.FileHandler(LOG, encoding="utf-8")])
    try:
        sys.exit(main())
    except Exception:
        log.exception("失敗")
        sys.exit(1)
