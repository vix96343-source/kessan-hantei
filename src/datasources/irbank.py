"""IRBANK 四半期進捗ページ(https://irbank.net/{code}/quarter)の「四半期毎履歴(百万円)」。

単独四半期(shihanki)の売上・営業利益・経常利益・純利益の「実績」行を、過去5〜6期分返す。
出典: IRBANK(元データは TDnet / EDINET)。再配信時は出典の明示が必要。
"""
import json
import logging
import re
from datetime import date, datetime
from pathlib import Path

from bs4 import BeautifulSoup

from .http import RateLimitedSession

log = logging.getLogger(__name__)

URL = "https://irbank.net/{code}/quarter"
MIN_INTERVAL_SEC = 2.0
FIELDS = ["sales", "op", "ordinary", "net"]
_Q = {"1Q": 1, "2Q": 2, "3Q": 3, "4Q": 4, "通期": 4}


def session_from_config(cfg: dict) -> RateLimitedSession:
    h = cfg["http"]
    return RateLimitedSession(h["user_agent"], max(MIN_INTERVAL_SEC, h["min_interval_sec"]),
                              h["retries"], h["timeout_sec"])


def _num(text: str) -> float | None:
    t = text.replace(",", "").replace("+", "").replace("△", "-").replace("▲", "-").strip()
    try:
        return float(t) * 1e6          # 百万円 → 円
    except ValueError:
        return None


def _shift_month(y: int, m: int, delta: int) -> str:
    i = y * 12 + (m - 1) + delta
    return f"{i // 12}-{i % 12 + 1:02d}"


def parse_quarterly(html: str) -> list[dict]:
    """[{period: '2027/03-1Q', end: '2026-06', sales, op, ordinary, net, announced: '2026-07-23'}, ...] 古い順"""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("table#graph")
    if table is None:
        return []
    out, fy = [], None
    # IRBANK の表は <tr> が閉じられていない行を含むため、セル単位で走査する
    for td in table.select("td.lf"):
        label = td.get_text(" ", strip=True)
        m = re.search(r"(\d{4})年\s*(\d{1,2})月期", label)
        if m:
            fy = (int(m.group(1)), int(m.group(2)))
            continue
        a = td.find("a")
        if fy is None or a is None or "実績" not in label:
            continue
        q = _Q.get(label.split()[0])
        if q is None:
            continue
        cells = []
        for sib in td.find_next_siblings("td"):
            if "lf" in (sib.get("class") or []):
                break
            cells.append(sib)
        vals = [_num(c.select_one(".shihanki").get_text()) if c.select_one(".shihanki") else None for c in cells[:4]]
        if len(vals) < 4:
            continue
        ann = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", a.get("title", ""))
        out.append({
            "period": f"{fy[0]}/{fy[1]:02d}-{q}Q",
            "end": _shift_month(fy[0], fy[1], -(4 - q) * 3),
            **dict(zip(FIELDS, vals)),
            "announced": f"{ann.group(1)}-{int(ann.group(2)):02d}-{int(ann.group(3)):02d}" if ann else "",
        })
    out.sort(key=lambda r: r["end"])
    # 同じ四半期が複数あれば(短信と四半期報告書など)後の行を残す
    dedup = {}
    for r in out:
        dedup[r["end"]] = r
    return list(dedup.values())


def fetch_quarterly(session: RateLimitedSession, code: str) -> list[dict]:
    r = session.get(URL.format(code=code))
    if r is None:
        return []
    r.encoding = "utf-8"
    return parse_quarterly(r.text)


# ---------------------------------------------------------------- キャッシュ

def cache_path(cache_dir: Path, code: str) -> Path:
    return cache_dir / f"{code}.json"


def get_quarterly(session: RateLimitedSession, code: str, cache_dir: Path,
                  max_age_days: int, today: date) -> list[dict]:
    """キャッシュが新しければそれを使う(当四半期は短信XBRLから計算するので前四半期まであれば十分)。"""
    p = cache_path(cache_dir, code)
    if p.exists():
        c = json.loads(p.read_text(encoding="utf-8"))
        if (today - date.fromisoformat(c["fetched"][:10])).days <= max_age_days:
            return c["quarters"]
    q = fetch_quarterly(session, code)
    cache_dir.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"fetched": datetime.now().isoformat(timespec="seconds"), "quarters": q},
                            ensure_ascii=False), encoding="utf-8")
    return q
