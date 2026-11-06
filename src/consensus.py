"""アナリスト予想の平均(コンセンサス)を取り、data/consensus.csv にためる。

- 株予報(kabuyoho.jp、IFIS コンセンサス): 今期・来期の通期の売上・営業利益・経常利益・当期利益(百万円)、
  経常利益の四半期累計(1Q・中間・3Q)。EPS は当期利益 ÷ 株数(site.py で短信の株数から出す)
- yfinance(米 Yahoo Finance): 株予報に無いときの EPS と、四半期(3か月)の売上・EPS
取れるのは担当アナリストがいる銘柄だけ。
発表前の値が「サプライズ」の比べる相手なので、決算予定(calendar.csv)の銘柄を毎日取り直して日付つきで残す。
"""
import logging
import math
import re
import time
from pathlib import Path

import pandas as pd
import requests
from bs4 import BeautifulSoup

from .config import DATA_DIR, now_jst

log = logging.getLogger(__name__)

KB_METRICS = ["sales", "op", "ord", "net"]
COLUMNS = ["code", "fetched_at", "n_0y", "eps_0y", "rev_0y", "n_1y", "eps_1y", "rev_1y",
           "n_0q", "eps_0q", "rev_0q", "n_1q", "eps_1q", "rev_1q",
           # 株予報: 決算期(YYYYMM)・通期の予想(百万円)・経常利益の四半期累計の予想・株予報の日付
           "kb_ym_0y", *[f"kb_{m}_0y" for m in KB_METRICS], "kb_ym_1y", *[f"kb_{m}_1y" for m in KB_METRICS],
           "kb_ord_q1", "kb_ord_q2", "kb_ord_q3", "kb_date"]
KB_ANALYST = "https://kabuyoho.jp/reportAnalyst?bcode={code}"
KB_REPORT = "https://kabuyoho.jp/report?bcode={code}"
KB_PAUSE = 2.0                                          # 株予報へは2秒に1回まで
NO_COVER_DAYS = 14                                      # 予想が無かった銘柄は2週間取り直さない


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


def _cells(tr) -> list[str]:
    return [re.sub(r"\s+", " ", c.get_text(" ", strip=True)) for c in tr.find_all(["th", "td"])]


def _val(t: str) -> float | None:
    m = re.match(r"\s*(-?[\d,]+(?:\.\d+)?)", t)
    return float(m.group(1).replace(",", "")) if m else None


def parse_kb_analyst(html: str) -> dict:
    """株予報「アナリスト予想」→ 決算期の表の「コ」(コンセンサス)の行。予想が無ければ {}"""
    s = BeautifulSoup(html, "html.parser")
    row, k = {}, 0
    for t in s.find_all("table"):
        trs = [_cells(tr) for tr in t.find_all("tr")]
        if not trs or trs[0][:5] != ["決算期", "売上高", "営業利益", "経常利益", "当期利益"]:
            continue
        for c in trs[1:]:
            m = re.match(r"(\d{6})\s*コ$", c[0]) if c else None
            if not m or k > 1:
                continue
            y = f"{k}y"
            row[f"kb_ym_{y}"] = m.group(1)
            for name, v in zip(KB_METRICS, c[1:5]):
                row[f"kb_{name}_{y}"] = _val(v)
            k += 1
    if row:
        d = re.search(r"日付\s*</t[hd]>\s*<t[hd][^>]*>\s*(\d{4}/\d{2}/\d{2})", html)
        row["kb_date"] = d.group(1).replace("/", "-") if d else ""
    return row


def parse_kb_report(html: str) -> dict:
    """株予報「業績」→ 経常利益のコンセンサスの四半期累計(1Q・中間・3Q)"""
    s = BeautifulSoup(html, "html.parser")
    for t in s.find_all("table"):
        trs = [_cells(tr) for tr in t.find_all("tr")]
        if not trs or trs[0][1:5] != ["第1四半期", "中間", "第3四半期", "通期"]:
            continue
        for c in trs:
            if c and c[0] == "コンセンサス予想":
                return {f"kb_ord_q{i}": _val(v) for i, v in zip((1, 2, 3), c[1:4])}
    return {}


def fetch_kb(session: requests.Session, code: str) -> dict:
    r = session.get(KB_ANALYST.format(code=code), timeout=20)
    r.raise_for_status()
    x = parse_kb_analyst(r.text)
    time.sleep(KB_PAUSE)
    if x:                                              # 予想がある会社だけ四半期累計も
        r = session.get(KB_REPORT.format(code=code), timeout=20)
        r.raise_for_status()
        x.update(parse_kb_report(r.text))
        time.sleep(KB_PAUSE)
    return x


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
    rows, yf_errors = [], 0
    t0 = time.monotonic()
    for c in codes:
        if c in recent:
            continue
        if time.monotonic() - t0 > time_budget_sec:
            log.info("コンセンサス: 時間切れ(残りは次回)")
            break
        x, failed = {}, False
        try:
            x = fetch_kb(session, c)
        except Exception as e:                         # 1銘柄の失敗で止めない
            log.warning("コンセンサス(株予報)取得失敗 %s: %s", c, e)
            failed = True
        if yf_errors < 3:                              # yfinance が続けて断られたら、この回はもう使わない
            try:
                x = {**x, **(fetch(c) or {})}
                yf_errors = 0
            except Exception as e:
                log.warning("コンセンサス(yfinance)取得失敗 %s: %s", c, e)
                yf_errors += 1
                failed = True
            time.sleep(pause_sec)
        if any(k != "code" for k in x):
            rows.append({**x, "code": c, "fetched_at": now.strftime("%Y-%m-%dT%H:%M")})
            none.pop(c, None)
            if len(rows) % 50 == 0:                    # 長い回は途中でも保存(止まっても取った分は残す)
                _save(df, rows, none, data_dir)
        elif not failed:                               # 取れたうえで予想が無い(担当アナリストがいない)
            none[c] = now.strftime("%Y-%m-%dT%H:%M")
    _save(df, rows, none, data_dir)
    log.info("consensus: %d / %d", len(rows), len(codes))
    return len(rows)


def _save(df: pd.DataFrame, rows: list[dict], none: dict[str, str], data_dir: Path) -> None:
    if rows:
        out = pd.concat([df, pd.DataFrame(rows).reindex(columns=COLUMNS)])
        out.sort_values(["code", "fetched_at"]).to_csv(path(data_dir), index=False, encoding="utf-8", lineterminator="\n")
    pd.Series(none, name="checked_at").rename_axis("code").sort_index().to_csv(
        _none_path(data_dir), encoding="utf-8", lineterminator="\n")


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
    """決算と比べるコンセンサス。株予報の値があればそれを優先: 発表より前に取った一番新しい値、
    無ければ発表のあとに取った一番早い値(発表後の値)。株予報が無ければ yfinance の値で、
    発表より前の一番新しい値(四半期の予想が無い古い記録しか無ければ発表後の一番新しい値)。"""
    has_kb = lambda r: str(r.get("kb_ym_0y", "")) not in ("", "nan")
    kb_before = [r for r in recs if has_kb(r) and r["fetched_at"] < disclosed_at]
    kb_after = [r for r in recs if has_kb(r) and r["fetched_at"] >= disclosed_at]
    if kb_before:
        return {**kb_before[-1], "before": True}
    if kb_after:
        return {**kb_after[0], "before": False}
    has_q = lambda r: any(str(r.get(k, "")) not in ("", "nan") for k in ("rev_0q", "eps_0q"))
    before = [r for r in recs if r["fetched_at"] < disclosed_at]
    after = [r for r in recs if r["fetched_at"] >= disclosed_at]
    if before and (has_q(before[-1]) or not any(has_q(r) for r in after)):
        return {**before[-1], "before": True}
    return {**after[-1], "before": False} if after else ({**before[-1], "before": True} if before else None)
