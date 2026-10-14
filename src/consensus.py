"""アナリスト予想の平均(コンセンサス)を yfinance(Yahoo Finance)から取り、data/consensus.csv にためる。

取れるのは担当アナリストがいる銘柄だけで、通期の売上と EPS(今期・来期)とアナリスト人数。
発表前の値が「サプライズ」の比べる相手なので、決算予定(calendar.csv)の銘柄を毎日取り直して日付つきで残す。
"""
import logging
import math
import time
from pathlib import Path

import pandas as pd

from .config import DATA_DIR, now_jst

log = logging.getLogger(__name__)

COLUMNS = ["code", "fetched_at", "n_0y", "eps_0y", "rev_0y", "n_1y", "eps_1y", "rev_1y",
           "n_0q", "eps_0q", "rev_0q", "n_1q", "eps_1q", "rev_1q"]


def path(data_dir: Path = DATA_DIR) -> Path:
    return data_dir / "consensus.csv"


def load(data_dir: Path = DATA_DIR) -> pd.DataFrame:
    p = path(data_dir)
    if not p.exists():
        return pd.DataFrame(columns=COLUMNS)
    return pd.read_csv(p, dtype={"code": str}, keep_default_na=False).reindex(columns=COLUMNS, fill_value="")


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or f == 0 else f


def fetch(code: str) -> dict | None:
    """1銘柄のコンセンサス。担当アナリストがいなければ None。"""
    import yfinance as yf
    t = yf.Ticker(f"{code}.T")
    e, r = t.earnings_estimate, t.revenue_estimate
    if e is None or e.empty:
        return None
    row = {"code": code}
    # 0y=今期・1y=来期(通期)、0q=今の四半期・1q=次の四半期(3か月)
    for k, key in (("0y", "0y"), ("1y", "+1y"), ("0q", "0q"), ("1q", "+1q")):
        row[f"n_{k}"] = _num(e["numberOfAnalysts"].get(key)) if "numberOfAnalysts" in e else None
        row[f"eps_{k}"] = _num(e["avg"].get(key))
        row[f"rev_{k}"] = _num(r["avg"].get(key)) if r is not None and not r.empty else None
    if not row.get("n_0y") or (row["eps_0y"] is None and row["rev_0y"] is None):
        return None
    return row


def update(cfg: dict, codes: list[str] | None = None, data_dir: Path = DATA_DIR,
           refresh_days: int = 1, pause_sec: float = 0.3, time_budget_sec: float = 1200) -> int:
    """codes(既定: 決算予定の銘柄。発表の近い順)のコンセンサスを取る。refresh_days 以内に取った銘柄は飛ばす。
    決算ピークは予定が数千件になるので time_budget_sec で打ち切る(残りは翌日)。"""
    from . import calendar_fetch
    if codes is None:
        cal = calendar_fetch.load(data_dir).sort_values("announce_date")
        codes = list(dict.fromkeys(cal["code"]))
    df = load(data_dir)
    now = now_jst()
    since = (now - pd.Timedelta(days=refresh_days)).strftime("%Y-%m-%dT%H:%M")
    recent = set(df.loc[df["fetched_at"] >= since, "code"])
    rows = []
    t0 = time.monotonic()
    for c in codes:
        if c in recent:
            continue
        if time.monotonic() - t0 > time_budget_sec:
            log.info("コンセンサス: 時間切れ(残りは次回)")
            break
        try:
            x = fetch(c)
        except Exception as e:                         # 1銘柄の失敗で止めない
            log.warning("コンセンサス取得失敗 %s: %s", c, e)
            x = None
        if x:
            rows.append({**x, "fetched_at": now.strftime("%Y-%m-%dT%H:%M")})
        time.sleep(pause_sec)
    if rows:
        out = pd.concat([df, pd.DataFrame(rows).reindex(columns=COLUMNS)])
        out.sort_values(["code", "fetched_at"]).to_csv(path(data_dir), index=False, encoding="utf-8")
    log.info("consensus: %d / %d", len(rows), len(codes))
    return len(rows)


def by_code(data_dir: Path = DATA_DIR) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in load(data_dir).sort_values("fetched_at").to_dict("records"):
        out.setdefault(r["code"], []).append(r)
    return out


def snapshot(recs: list[dict], disclosed_at: str) -> dict | None:
    """決算の発表より前に取った一番新しい値。四半期の予想が入っていない古い記録しか無ければ、発表後の一番新しい値。"""
    has_q = lambda r: str(r.get("rev_0q", "")) not in ("", "nan") or str(r.get("eps_0q", "")) not in ("", "nan")
    before = [r for r in recs if r["fetched_at"] < disclosed_at]
    after = [r for r in recs if r["fetched_at"] >= disclosed_at]
    if before and (has_q(before[-1]) or not any(has_q(r) for r in after)):
        return {**before[-1], "before": True}
    return {**after[-1], "before": False} if after else ({**before[-1], "before": True} if before else None)
