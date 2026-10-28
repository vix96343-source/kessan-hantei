"""判定(judge)の事後検証: 決算を出し終えた銘柄を、発表前に分かっていた情報だけで判定し直し、
実際の寄付ギャップ(発表直前の終値 → 発表翌営業日の寄付。EV の定義と同じ)と比べる → data/judge_backtest.csv

発表前の会社予想は TDnet の蓄積と IRBANK の補完(forecasts.py)から取る。補完が無い銘柄は「データ不足」になるので、
先に手元で `python -m src.cli judge-backtest --backfill` を実行する。
"""
import json
import logging
from datetime import date
from pathlib import Path

import pandas as pd

from . import bizdays, disclosures, forecasts, judge, universe
from .datasources import irbank_api
from .config import DATA_DIR, now_jst

log = logging.getLogger(__name__)

COLUMNS = ["code", "name", "market", "disclosed_at", "gap_class", "score_total", "conviction", "ev_pred_pct",
           "reason_quant", "ref_close", "next_open", "gap_open_pct", "hit"] + judge.FACTORS + ["computed_at"]


def path(data_dir: Path = DATA_DIR) -> Path:
    return data_dir / "judge_backtest.csv"


def load(data_dir: Path = DATA_DIR) -> pd.DataFrame:
    p = path(data_dir)
    if not p.exists():
        return pd.DataFrame(columns=COLUMNS)
    return pd.read_csv(p, dtype={"code": str}, keep_default_na=False).reindex(columns=COLUMNS, fill_value="")


def targets(cfg: dict, since: str, until: str, data_dir: Path = DATA_DIR) -> list[tuple[str, str]]:
    """since〜until(含む)に決算短信を出した、一次フィルタ(市場・売買代金)を通る銘柄 [(code, disclosed_at)]"""
    d = disclosures.load(data_dir)
    e = d[(d["kind"] == "earnings_report") & (d["is_correction"] != "True")
          & (d["disclosed_at"] >= since) & (d["disclosed_at"] < until + "T99")]
    u = universe.load(data_dir)[["code", "market", "avg_turnover_20d"]]
    e = e.merge(u, on="code")
    e = e[e["market"].isin(["プライム", "スタンダード"])
          & (pd.to_numeric(e["avg_turnover_20d"], errors="coerce") >= cfg["judge"]["min_avg_turnover"])]
    e = e.sort_values("disclosed_at").drop_duplicates("code")
    return list(zip(e["code"], e["disclosed_at"]))


def backfill(cfg: dict, tg: list[tuple[str, str]], data_dir: Path = DATA_DIR) -> dict:
    t = [(c, date.fromisoformat(at[:10])) for c, at in tg]
    return forecasts.backfill(cfg, t, judge.have_recent_snapshot(t, data_dir), data_dir)


# 決算以外の材料で株価が飛ぶ開示。同じ日に出ていれば検証から外す
# (子会社どうしの合併・子会社化のような自社株価への影響が小さいものは外さない)
OTHER_EVENTS = ("公開買付", "株式交換", "経営統合", "株式併合", "上場廃止", "MBO", "株式移転")


def other_event(code: str, disclosed_at: str, data_dir: Path = DATA_DIR) -> str:
    """同じ日に出た TOB などの開示の表題(無ければ空)。IRBANK の開示索引(手元)で調べる"""
    day = disclosed_at[:10]
    try:
        idx = irbank_api.load_index(data_dir, code)
    except Exception:
        return ""
    for r in idx.to_dict("records"):
        if r.get("disclosed_at") and forecasts._jst(r["disclosed_at"])[:10] == day                 and any(w in r.get("title", "") for w in OTHER_EVENTS) and "子会社" not in r.get("title", ""):
            return r["title"]
    return ""


def _prices(codes: list[str], start: str) -> dict[str, pd.DataFrame]:
    import yfinance as yf
    out = {}
    for i in range(0, len(codes), 100):
        part = codes[i:i + 100]
        try:
            d = yf.download([f"{c}.T" for c in part], start=start, group_by="ticker",
                            auto_adjust=False, threads=True, progress=False)
        except Exception as e:
            log.warning("yfinance取得失敗: %s", e)
            continue
        for c in part:
            try:
                df = (d[f"{c}.T"] if isinstance(d.columns, pd.MultiIndex) else d)[["Open", "Close"]]
            except KeyError:
                continue
            df = df.dropna(how="all")
            df.index = [x.date() for x in df.index]
            out[c] = df
    return out


def gap_of(df: pd.DataFrame, disclosed_at: str, close_time: str = "15:30") -> tuple[float, float] | None:
    """(発表直前の終値, 発表翌営業日の寄付)。場中の発表は前営業日の終値から"""
    ann = date.fromisoformat(disclosed_at[:10])
    ref_day = ann if disclosed_at[11:16] >= close_time else bizdays.add_business_days(ann, -1)
    nxt = bizdays.next_business_day(ann)
    c = df.loc[[d <= ref_day for d in df.index], "Close"].dropna()
    if c.empty or nxt not in df.index or pd.isna(df.loc[nxt, "Open"]):
        return None
    return float(c.iloc[-1]), float(df.loc[nxt, "Open"])


def run(cfg: dict, tg: list[tuple[str, str]], data_dir: Path = DATA_DIR) -> pd.DataFrame:
    if not tg:
        return load(data_dir)
    first = min(at for _, at in tg)[:10]
    start = (date.fromisoformat(first).replace(day=1) - pd.Timedelta(days=62)).isoformat()
    px = _prices(sorted({c for c, _ in tg}), start)
    closes = {c: df["Close"].dropna() for c, df in px.items()}
    max_jump = cfg["reaction"]["max_abs_ret_pct"]
    rows, at_now = [], now_jst().strftime("%Y-%m-%dT%H:%M")
    for code, at in tg:
        out = judge.judge(cfg, announce=date.fromisoformat(at[:10]), code=code, closes=closes,
                          data_dir=data_dir, write=False)
        g = gap_of(px[code], at) if code in px else None
        if out.empty or g is None:
            continue
        j = out.iloc[0]
        gap = (g[1] / g[0] - 1) * 100
        if abs(gap) > max_jump:                   # 分割未調整などの異常値
            continue
        ev_title = other_event(code, at, data_dir)
        if ev_title:
            log.info("検証から除外 %s: %s", code, ev_title)
            continue
        bd = json.loads(j["score_breakdown"])
        ev = float(j["ev_pred_pct"])
        rows.append({"code": code, "name": j["name"], "market": j["market"], "disclosed_at": at,
                     "gap_class": j["gap_class"], "score_total": float(j["score_total"]),
                     "conviction": j["conviction"], "ev_pred_pct": ev, "reason_quant": j["reason_quant"],
                     "ref_close": g[0], "next_open": g[1], "gap_open_pct": round(gap, 2),
                     "hit": int((ev > 0) == (gap > 0)) if ev != 0 and gap != 0 else "",
                     **{k: (round(bd[k]["v"], 3) if bd.get(k) else "") for k in judge.FACTORS},
                     "computed_at": at_now})
    new = pd.DataFrame(rows).reindex(columns=COLUMNS)
    old = load(data_dir)
    df = pd.concat([old[~old["code"].astype(str).str.cat(old["disclosed_at"].astype(str)).isin(
        new["code"].astype(str).str.cat(new["disclosed_at"].astype(str)))], new], ignore_index=True)
    df.sort_values(["disclosed_at", "code"], ascending=[False, True]).to_csv(path(data_dir), index=False,
                                                                           encoding="utf-8")
    log.info("backtest: %d 件", len(new))
    return df


def _rank_corr(a: pd.Series, b: pd.Series) -> float | None:
    """順位相関(スピアマン)。scipy を使わず順位の相関で計算する"""
    m = a.notna() & b.notna()
    if m.sum() < 5:
        return None
    v = a[m].rank().corr(b[m].rank())
    return None if pd.isna(v) else round(float(v), 3)


def summary(df: pd.DataFrame, cfg: dict) -> dict:
    """クラス別の件数・寄付が上がった割合・平均/中央値ギャップと、予想(較正テーブル)の勝率。
    因子ごとに「値が+のとき / −のとき」の平均ギャップも出す(どの因子が効いているか)。"""
    out = {"n": len(df), "classes": [], "factors": [], "corr": None}
    if df.empty:
        return out
    g = pd.to_numeric(df["gap_open_pct"], errors="coerce")
    s = pd.to_numeric(df["score_total"], errors="coerce")
    out["corr"] = _rank_corr(s, g)
    out["all"] = {"up": round(float((g > 0).mean() * 100), 1), "mean": round(float(g.mean()), 2)}
    for cls in ["★★GAP", "★GAP", "PASS", "CAUTION"]:
        m = df["gap_class"] == cls
        gg = g[m]
        out["classes"].append({
            "cls": cls, "n": int(m.sum()), "pred_win": cfg["judge"]["winrate_by_class"][cls],
            "up": round(float((gg > 0).mean() * 100), 1) if len(gg) else None,
            "mean": round(float(gg.mean()), 2) if len(gg) else None,
            "median": round(float(gg.median()), 2) if len(gg) else None,
        })
    for k in judge.FACTORS:
        v = pd.to_numeric(df[k], errors="coerce")
        if v.notna().sum() == 0:
            continue
        pos, neg = g[v > 0], g[v < 0]
        out["factors"].append({
            "name": k, "n": int(v.notna().sum()),
            "pos_n": int(len(pos)), "pos_mean": round(float(pos.mean()), 2) if len(pos) else None,
            "neg_n": int(len(neg)), "neg_mean": round(float(neg.mean()), 2) if len(neg) else None,
            "corr": _rank_corr(v, g),
        })
    return out
