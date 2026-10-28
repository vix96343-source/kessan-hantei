"""M3: 決算持ち越しの判定。翌営業日(または指定日)に決算を出す銘柄を、発表前に分かっていた数字だけで採点する。

一次フィルタ(軽い): 市場区分 ∈ {プライム, スタンダード}、平均売買代金 ≥ judge.min_avg_turnover、
  通過が多ければ株価を使わない仮スコアの上位 judge.max_candidates 件。
詳細スコア: 因子ごとの値 v ∈ [-1, 1] に config の重みを掛けて足す(factors.*.weight)。
  progress_gap          前回短信までの営業利益の進捗率 − 過去3年の同じ四半期の進捗率(通期実績比)の平均
  guidance_acceleration 会社予想が残り期間に織り込む前年比 − 足元の累計の前年比(会社が加速を予告しているか)
  guidance_conservatism 今期すでに出した業績予想の修正の向き(上方 − 下方)。過去3年分の修正癖は履歴が貯まってから
  momentum_pricing      直近20営業日のリターンと25日線乖離(正の重み: 事前に上げた銘柄ほど寄付も高かった。2026-10 の検証で反転)
  revision_recency      発表直前の業績予想の修正(負の重み: サプライズの余地が減る)
  beat_history          過去の会社計画超過の回数(未実装・重み0)
"""
import json
import logging
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from . import bizdays, calendar_fetch, disclosures, earnings, forecasts, history, revisions, universe
from .config import DATA_DIR, now_jst

log = logging.getLogger(__name__)

COLUMNS = [
    "run_date", "batch_id", "code", "name", "market", "announce_date", "announce_timing", "fiscal_q",
    "score_total", "score_breakdown", "ev_pred_pct", "winrate_pred_pct", "conviction", "gap_class",
    "reason_quant", "reason_qual", "price_at_pred",
    "gap_open_pct", "gap_close_pct", "ret_5d_pct", "hit_flag", "reviewed_at", "notes",
]
FACTORS = ["progress_gap", "guidance_acceleration", "guidance_conservatism", "momentum_pricing",
           "revision_recency", "beat_history"]
PRICE_FACTORS = {"momentum_pricing"}


def path(data_dir: Path = DATA_DIR) -> Path:
    return data_dir / "predictions.csv"


def load(data_dir: Path = DATA_DIR) -> pd.DataFrame:
    p = path(data_dir)
    if not p.exists():
        return pd.DataFrame(columns=COLUMNS)
    return pd.read_csv(p, dtype=str, keep_default_na=False).reindex(columns=COLUMNS, fill_value="")


def _num(v):
    if v is None or v == "" or (isinstance(v, float) and pd.isna(v)):
        return None
    return float(v)


def _clip(x: float) -> float:
    return max(-1.0, min(1.0, x))


def _ym(s: str) -> str:
    return str(s)[:7]


def _shift(ym: str, months: int) -> str:
    y, m = int(ym[:4]), int(ym[5:7])
    i = y * 12 + (m - 1) + months
    return f"{i // 12}-{i % 12 + 1:02d}"


# ---------------------------------------------------------------- 発表前の会社予想の状態

def snapshots(data_dir: Path = DATA_DIR) -> pd.DataFrame:
    """決算短信の時点の累計・会社予想(TDnet の蓄積 + IRBANK の補完)。列は forecasts.COLUMNS の earnings 部分"""
    a = earnings.load_archive(data_dir)
    t = pd.DataFrame({
        "disclosure_id": a["disclosure_id"], "code": a["code"], "disclosed_at": a["disclosed_at"],
        "n_q": a["n_q"], "period_end": a["period_end"], "scope": a["scope"],
        "cum_sales": a["cum_sales"], "cum_op": a["cum_op"], "prior_sales": a["prior_sales"], "prior_op": a["prior_op"],
        "fc_sales": a["fc_sales"], "fc_op": a["fc_op"], "fc_next_year": a["fc_next_year"].astype(str),
    })
    f = forecasts.load(data_dir)
    f = f[f["doc_kind"] == "earnings"]
    out = pd.concat([t, f.reindex(columns=t.columns.tolist() + ["fc_q2_sales", "fc_q2_op", "revised"])],
                    ignore_index=True)
    return out.drop_duplicates("disclosure_id", keep="last")


def revision_events(data_dir: Path = DATA_DIR) -> pd.DataFrame:
    """業績予想の修正(TDnet の修正XBRL。運用開始以降)。列: code, disclosed_at, period, cur_op, prev_op, cur_sales, prev_sales"""
    disc = disclosures.load(data_dir)[["disclosure_id", "code", "disclosed_at", "is_correction"]]
    rv = revisions.load(data_dir).merge(disc, on="disclosure_id", how="inner")
    rv = rv[rv["is_correction"] != "True"]
    return rv[["disclosure_id", "code", "disclosed_at", "period", "cur_op", "prev_op", "cur_sales", "prev_sales"]]


def _fy_end_of(snap: dict) -> str:
    """その短信の予想が対象とする期の期末(YYYY-MM)。本決算の短信の予想は来期"""
    n_q, pe = int(float(snap["n_q"])), _ym(snap["period_end"])
    return _shift(pe, 12) if n_q == 4 else _shift(pe, 3 * (4 - n_q))


def state_before(code: str, announce: date, snaps: pd.DataFrame, events: pd.DataFrame,
                 min_change: float = 0.005) -> dict | None:
    """発表日より前に分かっていた状態: 最新の短信 + その後の修正を反映した通期予想、今期の修正の向き。

    修正の向きは、今期の通期予想の推移(期初の本決算短信 → 各四半期の短信 → 修正開示)で
    隣り合う値が min_change 以上動いた回数を数える(営業利益、無ければ売上)。"""
    cut = announce.isoformat()
    s = snaps[(snaps["code"] == code) & (snaps["disclosed_at"] < cut)].sort_values("disclosed_at")
    if s.empty:
        return None
    snap = s.iloc[-1].to_dict()
    n_q = int(float(snap["n_q"]))
    fy_end = _fy_end_of(snap)

    points = [(r["disclosed_at"], 0, _num(r.get("fc_op")), _num(r.get("fc_sales")))
              for r in s.to_dict("records") if _fy_end_of(r) == fy_end]
    ev = events[(events["code"] == code) & (events["disclosed_at"] < cut) & (events["period"] == "CurrentYearDuration")
                & (events["disclosed_at"] >= points[0][0])]
    points += [(e.disclosed_at, 1, _num(e.cur_op), _num(e.cur_sales)) for e in ev.itertuples()]
    points.sort()                                  # 同時刻は短信 → 修正の順

    fc_op, fc_sales = _num(snap.get("fc_op")), _num(snap.get("fc_sales"))
    ups = downs = 0
    prev = None
    for _, _, op, sales in points:
        if op is not None:
            fc_op = op
        if sales is not None:
            fc_sales = sales
        val = (op, "op") if op is not None else (sales, "sales")
        if val[0] is None:
            continue
        if prev is not None and prev[1] == val[1] and prev[0] > 0:
            ch = val[0] / prev[0] - 1
            ups, downs = ups + (ch >= min_change), downs + (ch <= -min_change)
        prev = val
    # 前期の通期実績(本決算の短信)。四半期履歴の合計より正確(遡及修正などで合わないことがある)
    annual = [r for r in s.to_dict("records") if int(float(r["n_q"])) == 4 and _ym(r["period_end"]) == _shift(fy_end, -12)]
    ly_fy_op = _num(annual[-1].get("cum_op")) if annual else None
    return {"snap": snap, "n_q": n_q, "period_end": _ym(snap["period_end"]), "fy_end": fy_end,
            "fc_op": fc_op, "fc_sales": fc_sales, "ups": ups, "downs": downs, "ly_fy_op": ly_fy_op}


# ---------------------------------------------------------------- 因子

def _fy_quarters(hist: list[dict], fy_end: str) -> list[float | None]:
    """その期の 1Q〜4Q の単独営業利益(無い四半期は None)"""
    by_end = {q["end"]: q.get("op") for q in hist}
    return [by_end.get(_shift(fy_end, -9 + 3 * i)) for i in range(4)]


def progress_gap(st: dict, hist: list[dict], fcfg: dict) -> dict | None:
    n_q, fc, cum = st["n_q"], st["fc_op"], _num(st["snap"].get("cum_op"))
    if n_q >= 4 or fc is None or fc <= 0 or cum is None:
        return None
    past = []
    for k in range(1, fcfg.get("years", 3) + 1):
        qs = _fy_quarters(hist, _shift(st["fy_end"], -12 * k))
        if any(v is None for v in qs) or sum(qs) <= 0:
            continue
        p = sum(qs[:n_q]) / sum(qs) * 100
        # 通期がほぼゼロの年や累計が赤字の年は進捗率が意味を持たない(-109% のような値になる)ので使わない
        if not 0 <= p <= fcfg.get("max_progress_pct", 150):
            continue
        past.append(p)
    if len(past) < fcfg.get("min_years", 2):
        return None
    now = cum / fc * 100
    if not 0 <= now <= fcfg.get("max_progress_pct", 150):
        return None
    gap = now - sum(past) / len(past)
    return {"v": _clip(gap / fcfg["scale_pt"]), "progress": round(now, 1), "past_avg": round(sum(past) / len(past), 1),
            "gap_pt": round(gap, 1)}


def guidance_acceleration(st: dict, hist: list[dict], fcfg: dict) -> dict | None:
    """会社予想が残り期間に見込む前年比 − 足元の累計の前年比(pt)。
    本決算の直後(n_q=4)は「来期予想の前期比 − 終わった期の前期比」。"""
    n_q, fc = st["n_q"], st["fc_op"]
    cum, prior = _num(st["snap"].get("cum_op")), _num(st["snap"].get("prior_op"))
    # 累計が赤字(または前年が赤字)だと伸び率の符号が意味を持たない(-141% のような値になる)ので計算しない
    if fc is None or cum is None or prior is None or prior <= 0 or cum <= 0:
        return None
    if n_q == 4:
        rest_implied, rest_ly = fc, cum
    elif st.get("ly_fy_op") is not None:          # 前期の通期実績(短信) − 前年同期の累計(短信)
        rest_implied, rest_ly = fc - cum, st["ly_fy_op"] - prior
    else:
        ly = _fy_quarters(hist, _shift(st["fy_end"], -12))
        rest = ly[n_q:]
        if any(v is None for v in rest):
            return None
        rest_implied, rest_ly = fc - cum, sum(rest)
    if rest_ly <= 0:
        return None
    implied = (rest_implied / rest_ly - 1) * 100
    ytd = (cum / prior - 1) * 100
    acc = implied - ytd
    return {"v": _clip(acc / fcfg["scale_pt"]), "implied_yoy": round(implied, 1), "ytd_yoy": round(ytd, 1),
            "accel_pt": round(acc, 1)}


def guidance_conservatism(st: dict, fcfg: dict) -> dict:
    net = st["ups"] - st["downs"]
    return {"v": _clip(net / fcfg["scale_count"]), "ups": st["ups"], "downs": st["downs"]}


def momentum_pricing(closes: pd.Series | None, asof: date, fcfg: dict) -> dict | None:
    if closes is None:
        return None
    s = closes[[d <= asof for d in closes.index]].dropna()
    if len(s) < 26:
        return None
    last = float(s.iloc[-1])
    ret = (last / float(s.iloc[-21]) - 1) * 100
    dev = (last / float(s.tail(25).mean()) - 1) * 100
    v = _clip(0.5 * ret / fcfg["ret_scale_pct"] + 0.5 * dev / fcfg["dev_scale_pct"])
    return {"v": v, "ret_20d": round(ret, 1), "dev_25": round(dev, 1), "close": last}


def score(factors: dict[str, dict | None], cfg: dict) -> dict:
    jc, fw = cfg["judge"], cfg["factors"]
    total = 0.0
    used = possible = 0.0
    for k in FACTORS:
        w = float(fw.get(k, {}).get("weight", 0))
        possible += abs(w)
        f = factors.get(k)
        if f is not None and w:
            total += w * f["v"]
            used += abs(w)
    total = round(total, 4)
    coverage = used / possible if possible else 0.0
    th = jc["class_thresholds"]
    if coverage < jc["min_coverage"]:
        cls = "PASS"
    elif total >= th["gap2"]:
        cls = "★★GAP"
    elif total >= th["gap1"]:
        cls = "★GAP"
    elif total <= th["caution"]:
        cls = "CAUTION"
    else:
        cls = "PASS"
    return {"score_total": total, "coverage": round(coverage, 2), "gap_class": cls,
            "ev_pred_pct": round(total * jc["ev_per_score_pct"], 2),
            "winrate_pred_pct": jc["winrate_by_class"][cls],
            "conviction": "高" if coverage >= 0.85 else "中" if coverage >= jc["min_coverage"] else "低"}


def reason_text(st: dict, f: dict) -> str:
    parts = []
    q = f"{st['n_q']}Q" if st["n_q"] < 4 else "前期"
    if f.get("progress_gap"):
        p = f["progress_gap"]
        parts.append(f"{q}営業利益進捗{p['progress']}%(過去平均{p['past_avg']}%)")
    if f.get("guidance_acceleration"):
        a = f["guidance_acceleration"]
        what = "残り期間" if st["n_q"] < 4 else "今期"
        parts.append(f"会社予想は{what}に前年比{a['implied_yoy']:+.0f}%を織り込み(足元{a['ytd_yoy']:+.0f}%)")
    c = f.get("guidance_conservatism")
    if c and (c["ups"] or c["downs"]):
        parts.append(f"今期の修正 上方{c['ups']}回・下方{c['downs']}回")
    if f.get("momentum_pricing"):
        m = f["momentum_pricing"]
        parts.append(f"20日{m['ret_20d']:+.0f}%・25日線{m['dev_25']:+.0f}%")
    r = f.get("revision_recency")
    if r and r["v"]:
        parts.append(f"直前修正{r['last_date']}")
    return "、".join(parts) or "データ不足"


# ---------------------------------------------------------------- 実行

def targets_for(announce: date, cfg: dict, data_dir: Path = DATA_DIR, code: str | None = None) -> pd.DataFrame:
    """一次フィルタ: その日に発表予定のプライム・スタンダード銘柄で、売買代金が基準以上"""
    cal = calendar_fetch.load(data_dir)
    uni = universe.load(data_dir)[["code", "name", "market", "avg_turnover_20d"]]
    if code:
        c = cal[(cal["code"] == code) & (cal["announce_date"] == announce.isoformat())]
        base = c if not c.empty else pd.DataFrame([{"announce_date": announce.isoformat(), "code": code,
                                                   "announce_time": "", "announce_timing": "", "fiscal_q": ""}])
        return base.merge(uni, on="code", how="left").fillna("")
    c = cal[cal["announce_date"] == announce.isoformat()].merge(uni, on="code", how="inner")
    c = c[c["market"].isin(["プライム", "スタンダード"])]
    turnover = pd.to_numeric(c["avg_turnover_20d"], errors="coerce").fillna(0)
    return c[turnover >= cfg["judge"]["min_avg_turnover"]]


def judge(cfg: dict, announce: date | None = None, code: str | None = None, data_dir: Path = DATA_DIR,
          closes: dict[str, pd.Series] | None = None, asof: date | None = None, write: bool = True) -> pd.DataFrame:
    today = now_jst().date()
    announce = announce or bizdays.next_business_day(today)
    asof = asof or min(today, bizdays.add_business_days(announce, -1))
    tg = targets_for(announce, cfg, data_dir, code)
    if tg.empty:
        log.info("judge: %s に対象なし", announce)
        return pd.DataFrame(columns=COLUMNS)

    snaps, events = snapshots(data_dir), revision_events(data_dir)
    hist = history.by_code(history.load(data_dir))
    disc = disclosures.load(data_dir)
    cov = disclosures.load_meta(data_dir).get("coverage_start")
    fw = cfg["factors"]

    pre = []
    for t in tg.to_dict("records"):
        st = state_before(t["code"], announce, snaps, events)
        f = {k: None for k in FACTORS}
        if st:
            h = hist.get(t["code"], [])
            f["progress_gap"] = progress_gap(st, h, fw["progress_gap"])
            f["guidance_acceleration"] = guidance_acceleration(st, h, fw["guidance_acceleration"])
            f["guidance_conservatism"] = guidance_conservatism(st, fw["guidance_conservatism"])
        rr = disclosures.revision_recency(t["code"], asof, disc, fw["revision_recency"], cov)
        f["revision_recency"] = {"v": rr["value"], "last_date": rr["last_date"]} if rr["n_revisions"] else {"v": 0.0, "last_date": ""}
        pre.append((t, st, f, score({k: v for k, v in f.items() if k not in PRICE_FACTORS}, cfg)["score_total"]))

    # 二段目: 株価を使う因子は仮スコア上位だけ(ピーク日の全件取得を避ける)
    pre.sort(key=lambda x: x[3], reverse=True)
    pre = pre[: cfg["judge"]["max_candidates"]] if not code else pre
    if closes is None:
        from .datasources import yf
        closes = yf.daily_closes([t["code"] for t, *_ in pre], period="3mo")

    rows = []
    run_date = today.isoformat()
    for t, st, f, _ in pre:
        f["momentum_pricing"] = momentum_pricing(closes.get(t["code"]), asof, fw["momentum_pricing"])
        sc = score(f, cfg)
        breakdown = {k: (None if v is None else {**v, "v": round(v["v"], 3)}) for k, v in f.items()}
        price = f["momentum_pricing"]["close"] if f["momentum_pricing"] else ""
        rows.append({
            "run_date": run_date, "batch_id": run_date, "code": t["code"], "name": t.get("name", ""),
            "market": t.get("market", ""), "announce_date": announce.isoformat(),
            "announce_timing": t.get("announce_timing", ""), "fiscal_q": t.get("fiscal_q", "") or
            (f"Q{st['n_q'] % 4 + 1}" if st else ""),
            "score_total": sc["score_total"], "score_breakdown": json.dumps(breakdown, ensure_ascii=False),
            "ev_pred_pct": sc["ev_pred_pct"], "winrate_pred_pct": sc["winrate_pred_pct"],
            "conviction": sc["conviction"], "gap_class": sc["gap_class"],
            "reason_quant": reason_text(st, f) if st else "前回の決算短信のデータなし",
            "reason_qual": "", "price_at_pred": price,
        })
    out = pd.DataFrame(rows).reindex(columns=COLUMNS, fill_value="").sort_values("ev_pred_pct", ascending=False)
    if write:
        save(out, data_dir)
    return out


def upcoming_dates(cfg: dict, data_dir: Path = DATA_DIR) -> list[date]:
    """まだ発表されていない決算予定日(今日は場中の締め時刻 calendar.intraday_cutoff より前なら含める)"""
    now = now_jst()
    first = now.date() if now.strftime("%H:%M") < cfg["calendar"]["intraday_cutoff"] else now.date() + timedelta(days=1)
    cal = calendar_fetch.load(data_dir)
    return [date.fromisoformat(d) for d in sorted(set(cal["announce_date"])) if d >= first.isoformat()]


def judge_upcoming(cfg: dict, data_dir: Path = DATA_DIR, write: bool = True) -> pd.DataFrame:
    """決算予定にある、まだ発表されていない日をすべて判定する(株価は全日分を1回で取る)"""
    days = upcoming_dates(cfg, data_dir)
    codes = sorted({c for d in days for c in targets_for(d, cfg, data_dir)["code"]})
    if not codes:
        return pd.DataFrame(columns=COLUMNS)
    from .datasources import yf
    closes = yf.daily_closes(codes, period="3mo")
    outs = [judge(cfg, announce=d, data_dir=data_dir, closes=closes, write=write) for d in days]
    outs = [o for o in outs if not o.empty]
    return pd.concat(outs, ignore_index=True) if outs else pd.DataFrame(columns=COLUMNS)


def save(new: pd.DataFrame, data_dir: Path = DATA_DIR) -> None:
    """同じ batch_id × code はやり直しとして置き換える(答え合わせの列は消さない)"""
    df = load(data_dir)
    key = lambda d: d["batch_id"].astype(str) + "|" + d["code"].astype(str)  # noqa: E731
    old = df[key(df).isin(set(key(new)))].set_index(key(df)[key(df).isin(set(key(new)))])
    new = new.astype(str).copy()
    review_cols = ["gap_open_pct", "gap_close_pct", "ret_5d_pct", "hit_flag", "reviewed_at", "notes"]
    for i, k in enumerate(key(new)):
        if k in old.index:
            for c in review_cols:
                new.iloc[i, new.columns.get_loc(c)] = old.loc[k, c]
    out = pd.concat([df[~key(df).isin(set(key(new)))], new], ignore_index=True)
    out.sort_values(["run_date", "announce_date", "code"]).to_csv(path(data_dir), index=False, encoding="utf-8")


def backfill_targets(cfg: dict, data_dir: Path = DATA_DIR) -> list[tuple[str, date]]:
    """IRBANK で補う対象: 決算予定(全日程)のうち一次フィルタを通る銘柄"""
    cal = calendar_fetch.load(data_dir)
    out = []
    for d in sorted(set(cal["announce_date"])):
        ann = date.fromisoformat(d)
        out += [(c, ann) for c in targets_for(ann, cfg, data_dir)["code"]]
    return out


def have_recent_snapshot(targets: list[tuple[str, date]], data_dir: Path = DATA_DIR,
                         max_age_days: int = 120) -> set[str]:
    """発表日の max_age_days 日前以降に前回の短信がある銘柄(=補完不要)"""
    s = snapshots(data_dir)
    by = {c: sorted(g) for c, g in s.groupby("code")["disclosed_at"]}
    out = set()
    for c, ann in targets:
        lo, hi = (ann - timedelta(days=max_age_days)).isoformat(), ann.isoformat()
        if any(lo <= d < hi for d in by.get(c, [])):
            out.add(c)
    return out
