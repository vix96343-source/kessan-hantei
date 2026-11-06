"""アナリスト予想の平均(コンセンサス)を取り、data/consensus.csv にためる。

- Yahoo!ファイナンス(日本)の「業績予想」のアナリスト平均: 今期・来期の通期の売上・営業利益・経常利益・純利益(百万円)と人数
- yfinance(米 Yahoo Finance): EPS と、四半期(3か月)の売上・EPS
取れるのは担当アナリストがいる銘柄だけ(プライム・スタンダードの決算予定のおよそ4割。残りはどのサイトにも予想が無い)。
発表前の値が「サプライズ」の比べる相手なので、決算予定(calendar.csv)の銘柄を毎日取り直して日付つきで残す。
"""
import logging
import math
import re
import time
from pathlib import Path

import pandas as pd
import requests

from .config import DATA_DIR, now_jst

log = logging.getLogger(__name__)

YJ_METRICS = {"netSales": "sales", "operatingIncome": "op", "ordinaryIncome": "ord", "netProfit": "net"}
COLUMNS = ["code", "fetched_at", "n_0y", "eps_0y", "rev_0y", "n_1y", "eps_1y", "rev_1y",
           "n_0q", "eps_0q", "rev_0q", "n_1q", "eps_1q", "rev_1q",
           # Yahoo!ファイナンス(日本): 決算期(YYYYMM)・人数・通期の予想(百万円)・Yahoo の更新日
           "yj_ym_0y", "yj_n_0y", *[f"yj_{m}_0y" for m in YJ_METRICS.values()],
           "yj_ym_1y", "yj_n_1y", *[f"yj_{m}_1y" for m in YJ_METRICS.values()], "yj_updated"]
YJ_URL = "https://finance.yahoo.co.jp/quote/{code}.T/performance"
NO_COVER_DAYS = 7                                       # どちらにも予想が無かった銘柄は1週間取り直さない


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


def parse_yj(html: str) -> dict:
    """Yahoo!ファイナンスの業績ページ → 「業績予想(連結・確定)」のアナリスト平均。予想が無ければ {}"""
    t = html.replace('\\"', '"')
    row, k = {}, 0
    for m in re.finditer(r'\{"settlementYm":"(\d{6})","totalSampleCount":(\d+),([^{}]*)\}', t):
        if k > 1 or not int(m.group(2)):
            break
        y = f"{k}y"
        row[f"yj_ym_{y}"], row[f"yj_n_{y}"] = m.group(1), int(m.group(2))
        for name, v in re.findall(r'"(netSales|operatingIncome|ordinaryIncome|netProfit)":(-?[\d.]+)', m.group(3)):
            row[f"yj_{YJ_METRICS[name]}_{y}"] = round(float(v), 1)
        k += 1
    if row:
        d = re.search(r'ConsensusContainer__updateDate","dateTime":"([\d-]{10})', t)
        row["yj_updated"] = d.group(1) if d else ""
    return row


def fetch_yj(session: requests.Session, code: str) -> dict:
    r = session.get(YJ_URL.format(code=code), timeout=20)
    return parse_yj(r.text) if r.status_code == 200 else {}


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
    none = _load_none(data_dir)
    none_since = (now - pd.Timedelta(days=NO_COVER_DAYS)).strftime("%Y-%m-%dT%H:%M")
    recent |= {c for c, t in none.items() if t >= none_since}
    session = requests.Session()
    session.headers["User-Agent"] = cfg["http"]["user_agent"]
    rows = []
    t0 = time.monotonic()
    for c in codes:
        if c in recent:
            continue
        if time.monotonic() - t0 > time_budget_sec:
            log.info("コンセンサス: 時間切れ(残りは次回)")
            break
        x = {}
        try:
            x = fetch_yj(session, c)
            time.sleep(1.0)                            # Yahoo!ファイナンスは1秒に1回まで
        except Exception as e:                         # 1銘柄の失敗で止めない
            log.warning("コンセンサス(Yahoo!ファイナンス)取得失敗 %s: %s", c, e)
        try:
            x = {**x, **(fetch(c) or {})}
        except Exception as e:
            log.warning("コンセンサス(yfinance)取得失敗 %s: %s", c, e)
        if len(x) > 1 or (x and "code" not in x):
            rows.append({**x, "code": c, "fetched_at": now.strftime("%Y-%m-%dT%H:%M")})
            none.pop(c, None)
        else:
            none[c] = now.strftime("%Y-%m-%dT%H:%M")
        time.sleep(pause_sec)
    if rows:
        out = pd.concat([df, pd.DataFrame(rows).reindex(columns=COLUMNS)])
        out.sort_values(["code", "fetched_at"]).to_csv(path(data_dir), index=False, encoding="utf-8")
    pd.Series(none, name="checked_at").rename_axis("code").sort_index().to_csv(_none_path(data_dir), encoding="utf-8")
    log.info("consensus: %d / %d", len(rows), len(codes))
    return len(rows)


def _none_path(data_dir: Path) -> Path:
    return data_dir / "consensus_none.csv"             # 予想が無かった銘柄と確かめた時刻


def _load_none(data_dir: Path) -> dict[str, str]:
    p = _none_path(data_dir)
    if not p.exists():
        return {}
    d = pd.read_csv(p, dtype=str, keep_default_na=False)
    return dict(zip(d["code"], d["checked_at"]))


def by_code(data_dir: Path = DATA_DIR) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in load(data_dir).sort_values("fetched_at").to_dict("records"):
        out.setdefault(r["code"], []).append(r)
    return out


def snapshot(recs: list[dict], disclosed_at: str) -> dict | None:
    """決算の発表より前に取った一番新しい値。四半期の予想が入っていない古い記録しか無ければ、発表後の一番新しい値。"""
    has_q = lambda r: any(str(r.get(k, "")) not in ("", "nan") for k in ("rev_0q", "eps_0q", "yj_n_0y"))
    before = [r for r in recs if r["fetched_at"] < disclosed_at]
    after = [r for r in recs if r["fetched_at"] >= disclosed_at]
    if before and (has_q(before[-1]) or not any(has_q(r) for r in after)):
        return {**before[-1], "before": True}
    return {**after[-1], "before": False} if after else ({**before[-1], "before": True} if before else None)
