"""受注高の3か月の値の「1の誤差」を、決算説明資料の数値で直す。

短信の受注高は百万円未満切り捨ての累計なので、累計どうしを引いた3か月の値は ±1 ずれることがある
(ローツェ 2Q: 128,546 − 46,529 = 82,017 だが説明資料では 78,509 + … のように切り捨て前の値から出した数)。
説明資料には四半期ごとの値がそのまま載っていることが多いので、そのセグメント名が書かれたページに
計算値が無く、計算値 ±1 のどちらか一方だけがあれば、その数に置き換える。結果は data/order_adjust.json。
"""
import io
import json
import logging
import re
import unicodedata
from pathlib import Path

import pandas as pd

from . import disclosures, orders
from .config import DATA_DIR
from .datasources.http import RateLimitedSession

log = logging.getLogger(__name__)


def path(data_dir: Path = DATA_DIR) -> Path:
    return data_dir / "order_adjust.json"


def load(data_dir: Path = DATA_DIR) -> dict:
    p = path(data_dir)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"done": [], "adj": {}}


def _key(name: str) -> str:
    return re.sub(r"\s|・", "", unicodedata.normalize("NFKC", name))


def _ym_shift(ym: str, months: int) -> str:
    y, m = int(ym[:4]), int(ym[5:7]) + months
    y, m = y + (m - 1) // 12, (m - 1) % 12 + 1
    return f"{y:04d}-{m:02d}"


def singles(recs: list[dict], seg: str | None) -> dict[str, int]:
    """期末の年月 → その四半期の3か月の受注高(百万円・四捨五入前の円から)"""
    by = {str(r["period_end"])[:7]: r for r in recs if r.get("period_end")}

    def val(ym):
        r = by.get(ym)
        if not r:
            return None, None
        d = r["data"]
        x = d.get("total") if seg is None else next((s for s in d.get("segments", []) if _key(s["name"]) == seg), None)
        return (None if not x else x.get("orders")), (int(float(r["n_q"])) if r.get("n_q") else None)

    out = {}
    for ym in by:
        c, nq = val(ym)
        if c is None or nq is None:
            continue
        if nq == 1:
            out[ym] = round(c / 1e6)
            continue
        b, bq = val(_ym_shift(ym, -3))
        if b is not None and bq == nq - 1:
            out[ym] = round(c / 1e6) - round(b / 1e6)
    return out


def _pages(pdf: bytes) -> list[str]:
    from pypdf import PdfReader
    try:
        return [unicodedata.normalize("NFKC", p.extract_text() or "") for p in PdfReader(io.BytesIO(pdf)).pages]
    except Exception:
        return []


def adjust_from_pages(pages: list[str], names: dict[str, str], vals: dict[str, dict[str, int]]) -> dict:
    """names: セグメントの照合キー → 表示名、vals: キー → {期末: 3か月の値}。直す値 {キー: {期末: 値}}"""
    out = {}
    for k, label in names.items():
        on = [p for p in pages if k and k in re.sub(r"\s|・", "", p)]
        if not on:
            continue
        nums = [set(re.findall(r"(?<![\d,.])\d{1,3}(?:,\d{3})+(?![\d,])|(?<![\d,.])\d+(?![\d,.])", p)) for p in on]
        for ym, v in vals.get(k, {}).items():
            if abs(v) < 100:
                continue
            fmt = lambda x: f"{x:,}"
            if any(fmt(v) in ns for ns in nums):
                continue                                # 計算値がそのまま載っている
            hits = {c for c in (v - 1, v + 1) for ns in nums if fmt(c) in ns}
            if len(hits) == 1:
                out.setdefault(k, {})[ym] = hits.pop()
    return out


def update(cfg: dict, data_dir: Path = DATA_DIR, session: RateLimitedSession | None = None) -> int:
    """受注の表がある会社の、まだ見ていない決算説明資料(TDnet)を読み、1の誤差を直す値を足す。"""
    state = load(data_dir)
    done = set(state["done"])
    disc = disclosures.load(data_dir)
    pres = disc[disc["kind"] == "earnings_presentation"]
    by_code = orders.by_code(data_dir)
    session = session or RateLimitedSession.from_config(cfg)
    n = 0
    for code, recs in by_code.items():
        cur = [r for r in recs if "irbank_pdf" not in json.dumps(r["data"])]
        if not cur:
            continue
        last = cur[-1]
        cands = pres[(pres["code"] == code) & (pres["disclosed_at"] >= last["disclosed_at"][:10])]
        for p in cands.to_dict("records"):
            if p["disclosure_id"] in done:
                continue
            done.add(p["disclosure_id"])
            r = session.get(p["pdf_url"])
            pages = _pages(r.content) if r is not None else []
            if not pages:
                continue
            names = {_key(s["name"]): s["name"] for s in last["data"].get("segments", [])}
            # 説明資料に載るのは直近の数四半期だけ。古い期が偶然一致しても直さない
            floor = _ym_shift(str(last["period_end"])[:7], -15)
            vals = {k: {ym: v for ym, v in singles(recs, k).items() if ym >= floor} for k in names}
            adj = adjust_from_pages(pages, names, vals)
            if adj:
                state["adj"].setdefault(code, {})
                for k, m in adj.items():
                    state["adj"][code].setdefault(k, {}).update(m)
                n += sum(len(m) for m in adj.values())
                log.info("受注の1の誤差を説明資料で直す %s: %s", code, adj)
    state["done"] = sorted(done)
    path(data_dir).write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    return n
