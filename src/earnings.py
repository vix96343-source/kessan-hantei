"""決算短信の「上がりやすい決算内容」への親和性(3つの型)を数値化する。

① リクルート型: 売上の伸びが加速し、利益率も改善(q-accel と同じ: 単Q売上YoY − 直近1年売上YoY)
② キオクシア型: 単独四半期で前四半期から売上・営業益が急改善(季節性は前年の同じQoQと比べて補正)
③ ローツェ型 B: 予想を修正せず、好調なのに残り期間の想定利益が直近の実力より不自然に低い
③ ローツェ型 A(受注急増)・KPI・製品価格は短信の数値に無いため、文章を読むレイヤー(Phase 2)で扱う

データ: 当四半期 = 決算短信サマリーXBRL(開示と同時) / 過去の単独四半期 = IRBANK 四半期毎履歴(開示前に取得済みのもの)
"""
import logging
from datetime import date
from pathlib import Path

import pandas as pd

from . import disclosures, universe
from .config import DATA_DIR, now_jst
from .datasources import irbank, tdnet
from .datasources.http import RateLimitedSession

log = logging.getLogger(__name__)

FIELDS = ["sales", "op", "ordinary", "net"]
FEATURE_COLUMNS = [
    "disclosure_id", "code", "disclosed_at", "n_q", "period_end", "revised",
    "sales_q", "op_q", "sales_q_yoy", "sales_ttm_yoy", "accel", "margin", "margin_delta", "op_q_yoy",
    "sales_qoq", "op_qoq", "sales_qoq_ly", "conservatism",
    "recruit", "kioxia", "rorze_b", "types", "status", "computed_at",
]
TYPE_MARKS = {"recruit": "①", "kioxia": "②", "rorze_b": "③"}


def features_path(data_dir: Path = DATA_DIR) -> Path:
    return data_dir / "earnings_features.csv"


def load(data_dir: Path = DATA_DIR) -> pd.DataFrame:
    p = features_path(data_dir)
    if not p.exists():
        return pd.DataFrame(columns=FEATURE_COLUMNS)
    return pd.read_csv(p, dtype={"disclosure_id": str, "code": str, "types": str, "status": str},
                       keep_default_na=False).reindex(columns=FEATURE_COLUMNS, fill_value="")


# ---------------------------------------------------------------- 単独四半期の系列

def _month_index(ym: str) -> int:
    y, m = ym[:7].split("-")
    return int(y) * 12 + int(m)


def quarter_series(history: list[dict], x: dict, disclosed_date: str) -> list[dict] | None:
    """IRBANKの履歴(開示日より前に提出された分)+ 当四半期(XBRL累計から差し引き)を古い順に返す。

    履歴の最後が当四半期のちょうど3ヶ月前に終わっていなければ None(期ずれ・決算期変更)。
    """
    hist = [h for h in history if h.get("announced") and h["announced"] < disclosed_date]
    end = _month_index(x["period_end"])
    hist = [h for h in hist if _month_index(h["end"]) < end]
    if not hist or end - _month_index(hist[-1]["end"]) != 3:
        return None
    n_q = x["n_q"]
    prev_in_fy = hist[-(n_q - 1):] if n_q > 1 else []
    if len(prev_in_fy) != n_q - 1:
        return None
    cur = {"end": x["period_end"][:7]}
    for f in FIELDS:
        c = x["cum"].get(f)
        prev = [h.get(f) for h in prev_in_fy]
        cur[f] = None if c is None or any(p is None for p in prev) else c - sum(prev)
    return hist + [cur]


def _pct(a, b):
    if a is None or b is None or b <= 0:
        return None
    return (a / b - 1) * 100


def _ratio(a, b):
    if a is None or b is None or b <= 0:
        return None
    return a / b


# ---------------------------------------------------------------- 特徴量と型判定

def compute(series: list[dict], x: dict, tcfg: dict) -> dict:
    cur = series[-1]
    q = lambda i, f: series[i][f] if len(series) >= -i and series[i].get(f) is not None else None  # noqa: E731

    f = {"sales_q": cur.get("sales"), "op_q": cur.get("op")}
    # ① 加速: 単Q売上YoY − 直近4四半期合計の売上YoY
    f["sales_q_yoy"] = _pct(q(-1, "sales"), q(-5, "sales"))
    if len(series) >= 8 and all(s.get("sales") is not None for s in series[-8:]):
        f["sales_ttm_yoy"] = _pct(sum(s["sales"] for s in series[-4:]), sum(s["sales"] for s in series[-8:-4]))
    else:
        f["sales_ttm_yoy"] = None
    f["accel"] = None if f["sales_q_yoy"] is None or f["sales_ttm_yoy"] is None else f["sales_q_yoy"] - f["sales_ttm_yoy"]
    margin = lambda i: _ratio(q(i, "op"), q(i, "sales"))  # noqa: E731
    f["margin"] = None if margin(-1) is None else margin(-1) * 100
    f["margin_delta"] = None if margin(-1) is None or margin(-5) is None else (margin(-1) - margin(-5)) * 100
    f["op_q_yoy"] = _pct(q(-1, "op"), q(-5, "op"))

    # ② QoQ と前年の同じQoQ(季節性)
    f["sales_qoq"] = _pct(q(-1, "sales"), q(-2, "sales"))
    f["sales_qoq_ly"] = _pct(q(-5, "sales"), q(-6, "sales"))
    prev_op, cur_op = q(-2, "op"), q(-1, "op")
    if prev_op is not None and cur_op is not None and prev_op <= 0 < cur_op:
        f["op_qoq"] = 999.0                       # 赤字→黒字転換は急改善として扱う
    else:
        f["op_qoq"] = _pct(cur_op, prev_op)

    # ③B 慎重度: 会社予想が想定する残り期間の営業益 ÷ (前年の同じ期間の実績 × 今期の伸び)
    f["conservatism"] = _conservatism(series, x)

    f["recruit"] = bool(f["accel"] is not None and f["accel"] >= tcfg["recruit"]["min_accel"]
                        and f["margin_delta"] is not None and f["margin_delta"] >= tcfg["recruit"]["min_margin_delta"])
    # 季節性: 前年の同じQoQより min_qoq_excess pt 以上強いこと(前年データが無ければ問わない)
    seasonal_ok = f["sales_qoq"] is not None and (
        f["sales_qoq_ly"] is None or f["sales_qoq"] - f["sales_qoq_ly"] >= tcfg["kioxia"]["min_qoq_excess"])
    f["kioxia"] = bool(f["sales_qoq"] is not None and f["sales_qoq"] >= tcfg["kioxia"]["min_sales_qoq"]
                       and f["op_qoq"] is not None and f["op_qoq"] >= tcfg["kioxia"]["min_op_qoq"] and seasonal_ok)
    f["rorze_b"] = bool(x.get("revised") is False and f["op_q_yoy"] is not None and f["op_q_yoy"] > 0
                        and f["conservatism"] is not None and f["conservatism"] <= tcfg["rorze_b"]["max_conservatism"])
    f["types"] = "".join(m for k, m in TYPE_MARKS.items() if f[k])
    return f


def _conservatism(series: list[dict], x: dict) -> float | None:
    n_q, fc, cum, prior = x["n_q"], x["forecast"].get("op"), x["cum"].get("op"), x["prior_cum"].get("op")
    if n_q == 1 and x.get("forecast_q2", {}).get("op") is not None:
        # 上期予想 − 1Q実績 = 2Q想定 を、前年2Q実績 × 1Qの伸び と比べる
        implied = x["forecast_q2"]["op"] - cum
        ly = series[-4].get("op") if len(series) >= 4 else None
        growth = _ratio(cum, prior)
        return _ratio(implied, ly * growth) if ly and growth else None
    if fc is None or cum is None:
        return None
    if n_q == 4:
        # 来期予想 ÷ (今期実績 × 直近四半期の伸び)
        growth = _ratio(series[-1].get("op"), series[-5].get("op")) if len(series) >= 5 else None
        return _ratio(fc, cum * growth) if growth else None
    implied = fc - cum                                 # 残り (4 − n_q) 四半期の想定営業益
    ly_rest = [s.get("op") for s in series[-4:-n_q]] if len(series) >= 4 else []
    growth = _ratio(cum, prior)
    if len(ly_rest) != 4 - n_q or any(v is None for v in ly_rest) or not growth:
        return None
    return _ratio(implied, sum(ly_rest) * growth)


def evaluate(code: str, disclosed_at: str, x: dict, history: list[dict], tcfg: dict) -> dict:
    row = {"code": code, "disclosed_at": disclosed_at, "n_q": x.get("n_q", ""), "period_end": x.get("period_end", ""),
           "revised": "" if x.get("revised") is None else str(x["revised"])}
    if not x:
        return {**row, "status": "no_xbrl"}
    series = quarter_series(history, x, disclosed_at[:10])
    if series is None:
        return {**row, "status": "no_history"}
    f = compute(series, x, tcfg)
    return {**row, **{k: (round(v, 2) if isinstance(v, float) else v) for k, v in f.items()}, "status": "ok"}


# ---------------------------------------------------------------- 取り込み

def update(cfg: dict, data_dir: Path = DATA_DIR, tdnet_session: RateLimitedSession | None = None,
           history_session: RateLimitedSession | None = None, limit: int | None = None,
           today: date | None = None) -> dict:
    """未評価の決算短信(universe内・XBRLあり)を評価して earnings_features.csv に追記する。"""
    ecfg = cfg["earnings"]
    today = today or now_jst().date()
    tdnet_session = tdnet_session or RateLimitedSession.from_config(cfg)
    history_session = history_session or irbank.session_from_config(cfg)
    cache_dir = data_dir / "cache" / "irbank"

    disc = disclosures.load(data_dir)
    uni = set(universe.load(data_dir)["code"])
    feats = load(data_dir)
    done = set(feats.loc[feats["status"].isin(["ok", "no_xbrl"]), "disclosure_id"])
    todo = disc[(disc["kind"] == "earnings_report") & (disc["is_correction"] != "True")
                & disc["code"].isin(uni) & ~disc["disclosure_id"].isin(done)]
    todo = todo.sort_values("disclosed_at", ascending=False).head(limit or ecfg["max_per_run"])

    rows = []
    for r in todo.itertuples():
        try:
            x = tdnet.fetch_earnings(tdnet_session, r.xbrl_url) if r.xbrl_url else {}
            hist = irbank.get_quarterly(history_session, r.code, cache_dir, ecfg["history_max_age_days"], today)
        except Exception as e:                       # 1件の失敗で止めない。次回再試行
            log.warning("決算評価失敗 %s %s: %s", r.code, r.disclosure_id, e)
            continue
        rows.append({"disclosure_id": r.disclosure_id,
                     **evaluate(r.code, r.disclosed_at, x, hist, ecfg["types"]),
                     "computed_at": now_jst().strftime("%Y-%m-%dT%H:%M")})

    if rows:
        new = pd.DataFrame(rows).reindex(columns=FEATURE_COLUMNS, fill_value="")
        feats = pd.concat([feats[~feats["disclosure_id"].isin(new["disclosure_id"])], new])
        feats = feats.sort_values(["disclosed_at", "disclosure_id"], ascending=False)
        feats.to_csv(features_path(data_dir), index=False, encoding="utf-8")
    stats = {"evaluated": len(rows), "remaining": max(0, len(todo) - len(rows)),
             "status": pd.Series([r["status"] for r in rows]).value_counts().to_dict() if rows else {}}
    log.info("earnings: %s", stats)
    return stats


def prefetch_history(cfg: dict, codes: list[str], data_dir: Path = DATA_DIR,
                     session: RateLimitedSession | None = None, today: date | None = None) -> int:
    """発表予定の銘柄の履歴を前日のうちにIRBANKから取得しておく(当日は短信XBRLだけで計算できる)。"""
    session = session or irbank.session_from_config(cfg)
    today = today or now_jst().date()
    n = 0
    for c in codes:
        try:
            irbank.get_quarterly(session, c, data_dir / "cache" / "irbank",
                                 cfg["earnings"]["history_max_age_days"], today)
            n += 1
        except Exception as e:
            log.warning("IRBANK取得失敗 %s: %s", c, e)
    return n


def by_disclosure(data_dir: Path = DATA_DIR) -> dict[str, dict]:
    return {r["disclosure_id"]: r for r in load(data_dir).to_dict("records")}
