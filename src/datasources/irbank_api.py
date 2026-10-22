"""IRBANK Connect の REST API(https://api.irbank.net/v1)。手元でのみ使う(キーは .env の IRBANK_API_KEY)。

会社ごとの開示一覧(決算短信・決算説明資料の書類番号と PDF の URL)を取り、~/kessan-pdfs/index/{コード}.csv にためる。
上限は 1 分 60 回・1 日 4,000 回(MCP と合算、毎日 0:00 JST にリセット)なので、1.1 秒おきに呼び、
その日の回数を ~/kessan-pdfs/irbank_quota.json に数えて 3,900 回で止める。PDF の取得(api.irbank.net/api/market-files)は
認証が要らず、この回数には入らない。
"""
import json
import logging
import os
import time
from pathlib import Path

import pandas as pd
import requests

from ..config import DATA_DIR, now_jst

log = logging.getLogger(__name__)

BASE = "https://api.irbank.net/v1"
INDEX_COLUMNS = ["code", "doc_id", "source", "filing_date", "disclosed_at", "title", "pdf_url", "fetched_at"]
# 索引・回数・キーは、作業用の repo と自動補完用の clone(kessan-hantei-auto)で共有する
SHARED = Path.home() / "kessan-pdfs"
DAILY_CAP = 3900                                       # 1 日 4,000 回のうち、MCP で使う分を少し残す
MIN_INTERVAL = 1.1                                     # 1 分 60 回より少し遅く


class QuotaExceeded(Exception):
    """その日の上限に達した(翌日 0:00 JST 以降に続きから)"""


def api_key(root: Path = DATA_DIR.parent) -> str | None:
    if os.environ.get("IRBANK_API_KEY"):
        return os.environ["IRBANK_API_KEY"]
    for env in (root / ".env", SHARED / ".env"):
        if env.exists():
            for line in env.read_text(encoding="utf-8").splitlines():
                if line.startswith("IRBANK_API_KEY="):
                    return line.split("=", 1)[1].strip()
    return None


class Client:
    def __init__(self, key: str | None = None, data_dir: Path = DATA_DIR):
        self.key = key or api_key()
        if not self.key:
            raise RuntimeError("IRBANK_API_KEY がありません(.env に書く)")
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {self.key}", "User-Agent": "kessan-hantei"})
        self.quota_path = SHARED / "irbank_quota.json"
        self._last = 0.0

    def _quota(self) -> dict:
        today = now_jst().strftime("%Y-%m-%d")
        try:
            q = json.loads(self.quota_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            q = {}
        return q if q.get("date") == today else {"date": today, "used": 0}

    def _count(self) -> None:
        q = self._quota()
        if q["used"] >= DAILY_CAP:
            raise QuotaExceeded(f"今日の上限 {DAILY_CAP} 回")
        q["used"] += 1
        self.quota_path.parent.mkdir(parents=True, exist_ok=True)
        self.quota_path.write_text(json.dumps(q), encoding="utf-8")

    def get(self, path: str, params: dict | None = None) -> dict | None:
        for attempt in range(5):
            wait = MIN_INTERVAL - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._count()
            self._last = time.monotonic()
            r = self.s.get(f"{BASE}/{path}", params=params, timeout=30)
            if r.status_code == 404:
                return None
            if r.status_code == 429:                   # 1 分の上限: リセットまで待つ
                reset = int(r.headers.get("x-ratelimit-reset", "0") or 0)
                time.sleep(max(5, min(70, reset - time.time() + 1)))
                continue
            if r.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            return r.json()
        raise RuntimeError(f"IRBANK API 失敗: {path}")

    def securities(self) -> list[dict]:
        """上場銘柄の一覧(ETF などを含む)"""
        out, cursor = [], None
        while True:
            d = self.get("securities", {"limit": 100, **({"cursor": cursor} if cursor else {})})
            out += d.get("securities", [])
            cursor = d.get("next_cursor")
            if not cursor:
                return out

    def disclosures(self, code: str, since: str) -> list[dict]:
        """その銘柄の since(YYYY-MM-DD)以降の開示(新しい順)"""
        out, cursor = [], None
        while True:
            d = self.get(f"securities/{code}/disclosures",
                         {"from": since, "limit": 100, **({"cursor": cursor} if cursor else {})})
            if not d:
                return out
            out += d.get("disclosures", [])
            cursor = d.get("next_cursor")
            if not cursor:
                return out


# ---------------------------------------------------------------- 開示一覧の索引(手元)

def index_dir() -> Path:
    """銘柄ごとの索引ファイルの置き場(作業用の repo・自動補完の clone・一括取得が同時に書いても壊れないよう1銘柄1ファイル)"""
    return SHARED / "index"


def _code_path(code: str) -> Path:
    return index_dir() / f"{code}.csv"


def load_index(data_dir: Path = DATA_DIR, code: str | None = None) -> pd.DataFrame:
    """索引(code を渡すとその銘柄だけ)"""
    files = [_code_path(code)] if code else sorted(index_dir().glob("*.csv"))
    frames = [pd.read_csv(f, dtype=str, keep_default_na=False) for f in files if f.exists()]
    if not frames:
        return pd.DataFrame(columns=INDEX_COLUMNS)
    return pd.concat(frames).reindex(columns=INDEX_COLUMNS).fillna("")


def indexed_codes() -> set[str]:
    return {f.stem for f in index_dir().glob("*.csv")}


def append_index(code: str, rows: list[dict], data_dir: Path = DATA_DIR) -> None:
    """その銘柄の索引を書き直す(空でも「取った」印として1行残す)。一時ファイルに書いてから置き換える"""
    at = now_jst().strftime("%Y-%m-%dT%H:%M")
    new = pd.DataFrame([{**{k: r.get(k) or "" for k in INDEX_COLUMNS}, "code": code, "fetched_at": at} for r in rows]
                       or [{"code": code, "fetched_at": at}]).reindex(columns=INDEX_COLUMNS).fillna("")
    index_dir().mkdir(parents=True, exist_ok=True)
    tmp = index_dir() / f".{code}.{os.getpid()}.tmp"
    new.to_csv(tmp, index=False, encoding="utf-8")
    os.replace(tmp, _code_path(code))


def earnings_reports(code: str, data_dir: Path = DATA_DIR, fetch: bool = True,
                     since: str = "2024-01-01", fresh_after: str = "") -> list[dict] | None:
    """その銘柄の TDnet の開示(索引から。無い・fresh_after より前に取った索引なら API で取り直して索引に足す)。
    キーが無い・上限なら索引のまま(無ければ None)"""
    rows = load_index(data_dir, code)
    stale = not rows.empty and fresh_after and rows["fetched_at"].max() < fresh_after
    if rows.empty or stale:
        if not fetch:
            return None
        try:
            got = Client(data_dir=data_dir).disclosures(code, since)
        except (RuntimeError, QuotaExceeded, requests.RequestException) as e:
            log.warning("IRBANK API: %s %s", code, e)
            if rows.empty:
                return None
            return [r for r in rows.to_dict("records") if r["doc_id"] and r["source"] == "tdnet"]
        append_index(code, got, data_dir)
        rows = load_index(data_dir, code)
    return [r for r in rows.to_dict("records") if r["doc_id"] and r["source"] == "tdnet"]
