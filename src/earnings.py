"""決算短信の「上がりやすい決算内容」への親和性(3つの型)を、TDnet の決算短信XBRLだけで数値化する。

① リクルート型: 売上の伸びが加速し、利益率も改善
   = 今期累計の売上YoY − 前年同期の売上YoY(短信に載っている去年の伸び率)≥ 3pt かつ 営業利益率の前年同期差 ≥ 0
   1Qは累計=単独四半期なので q-accel と同じ単Qベース。2Q以降は累計ベース。
② キオクシア型: 単独四半期で前四半期から売上・営業益が急改善(季節性は前年の同じQoQと比べて補正)
   単独四半期 = 今回の累計 − 前回の短信の累計。前回の短信を tdnet_earnings.csv に自前で蓄積しておき、
   揃っている銘柄だけ判定する(TDnet は31日で消えるため、蓄積は運用開始から)。
③ ローツェ型 B: 予想を修正せず、好調なのに残り期間の想定利益が慎重
   前期の通期実績 = 会社予想 ÷ (1 + 予想の前期比)。残り期間の想定 = 予想 − 累計。
   慎重度 = 残り期間の想定 ÷ (前年の残り期間の実績 × 今期累計の伸び)
③ ローツェ型 A(受注急増)・KPI・製品価格は短信の数値に無いため、文章を読むレイヤー(Phase 2)で扱う
"""
import logging
from pathlib import Path

import pandas as pd

from . import disclosures, history
from .config import DATA_DIR, now_jst
from .datasources import tdnet
from .datasources.http import RateLimitedSession

log = logging.getLogger(__name__)

FIELDS = ["sales", "op", "ordinary", "net"]
FEATURE_COLUMNS = [
    "disclosure_id", "code", "disclosed_at", "n_q", "period_end", "revised",
    "sales_ytd_yoy", "sales_ytd_yoy_ly", "accel", "margin", "margin_delta", "op_ytd_yoy",
    "sales_q", "op_q", "sales_qoq", "op_qoq", "sales_qoq_ly", "conservatism", "progress",
    "recruit", "kioxia", "rorze_b", "types", "status", "computed_at",
]
ARCHIVE_COLUMNS = ["disclosure_id", "code", "disclosed_at", "n_q", "period_end", "scope"] + \
    [f"cum_{f}" for f in FIELDS] + [f"prior_{f}" for f in FIELDS] + \
    [f"fc_{f}" for f in FIELDS] + ["fc_next_year"]          # 会社予想(通期。本決算の短信は来期)
TYPE_MARKS = {"recruit": "①", "kioxia": "②", "rorze_b": "③"}


def features_path(data_dir: Path = DATA_DIR) -> Path:
    return data_dir / "earnings_features.csv"


def archive_path(data_dir: Path = DATA_DIR) -> Path:
    return data_dir / "tdnet_earnings.csv"


def load(data_dir: Path = DATA_DIR) -> pd.DataFrame:
    p = features_path(data_dir)
    if not p.exists():
        return pd.DataFrame(columns=FEATURE_COLUMNS)
    return pd.read_csv(p, dtype={"disclosure_id": str, "code": str, "types": str, "status": str},
                       keep_default_na=False).reindex(columns=FEATURE_COLUMNS, fill_value="")


def load_archive(data_dir: Path = DATA_DIR) -> pd.DataFrame:
    p = archive_path(data_dir)
    if not p.exists():
        return pd.DataFrame(columns=ARCHIVE_COLUMNS)
    return pd.read_csv(p, dtype={"disclosure_id": str, "code": str, "period_end": str},
                       keep_default_na=False).reindex(columns=ARCHIVE_COLUMNS)


def by_disclosure(data_dir: Path = DATA_DIR) -> dict[str, dict]:
    return {r["disclosure_id"]: r for r in load(data_dir).to_dict("records")}


# ---------------------------------------------------------------- 計算の部品

def _pct(a, b):
    if a is None or b is None or b <= 0:
        return None
    return (a / b - 1) * 100


def _ratio(a, b):
    if a is None or b is None or b <= 0:
        return None
    return a / b


def _num(v):
    if v is None or v == "" or (isinstance(v, float) and pd.isna(v)):
        return None
    return float(v)


def _month(period_end: str) -> str:
    return str(period_end)[:7]


def _shift(ym: str, months: int) -> str:
    y, m = int(ym[:4]), int(ym[5:7])
    i = y * 12 + (m - 1) + months
    return f"{i // 12}-{i % 12 + 1:02d}"


def archive_row(disclosure_id: str, code: str, disclosed_at: str, x: dict) -> dict:
    return {"disclosure_id": disclosure_id, "code": code, "disclosed_at": disclosed_at, "n_q": x["n_q"],
            "period_end": x["period_end"], "scope": x["scope"],
            **{f"cum_{f}": x["cum"].get(f) for f in FIELDS}, **{f"prior_{f}": x["prior_cum"].get(f) for f in FIELDS},
            **{f"fc_{f}": x["forecast"].get(f) for f in FIELDS}, "fc_next_year": bool(x.get("forecast_is_next_year"))}


def archive_index(rows: list[dict]) -> dict[tuple[str, str], dict]:
    """(code, 'YYYY-MM') -> 短信1件。同じ四半期が複数あれば後の開示(訂正後)を使う。"""
    idx = {}
    for r in sorted(rows, key=lambda r: str(r["disclosed_at"])):
        idx[(r["code"], _month(r["period_end"]))] = r
    return idx


def single_quarter(idx: dict, code: str, ym: str, field: str, which: str = "cum") -> float | None:
    """単独四半期の値。which='cum' は今期、'prior' は前年同期。1Qは累計=単独。
    2Q以降は1つ前の四半期の短信(同じ期で n_q が1つ小さい)が必要。"""
    r = idx.get((code, ym))
    if r is None:
        return None
    v = _num(r.get(f"{which}_{field}"))
    if v is None:
        return None
    if int(r["n_q"]) == 1:
        return v
    prev = idx.get((code, _shift(ym, -3)))
    if prev is None or int(prev["n_q"]) != int(r["n_q"]) - 1 or prev["scope"] != r["scope"]:
        return None
    pv = _num(prev.get(f"{which}_{field}"))
    return None if pv is None else v - pv


# ---------------------------------------------------------------- 特徴量と型判定

def conservatism(x: dict) -> float | None:
    """③B 慎重度。1未満ほど、会社予想が残り期間に前年より弱い伸びしか見込んでいない。"""
    n_q, cum, prior = x["n_q"], x["cum"].get("op"), x["prior_cum"].get("op")
    growth = _ratio(cum, prior)
    if n_q == 4 or growth is None:
        return None
    if n_q == 1 and x.get("forecast_q2", {}).get("op") is not None:
        fc, chg = x["forecast_q2"]["op"], x.get("forecast_q2_change", {}).get("op")
    else:
        fc, chg = x["forecast"].get("op"), x.get("forecast_change", {}).get("op")
    if fc is None or chg is None or chg <= -100:
        return None
    prior_full = fc / (1 + chg / 100)          # 前期(1Qで上期予想を使う場合は前年上期)の実績
    ly_rest = prior_full - prior                # 前年の残り期間の実績
    implied = fc - cum                          # 会社予想が想定する残り期間
    if ly_rest <= 0:
        return None
    return implied / (ly_rest * growth)


def compute(x: dict, idx: dict, code: str, tcfg: dict) -> dict:
    cum, prior, pchg = x["cum"], x["prior_cum"], x.get("prior_change", {})
    f = {}
    # ① 加速と利益率
    f["sales_ytd_yoy"] = _pct(cum.get("sales"), prior.get("sales"))
    f["sales_ytd_yoy_ly"] = pchg.get("sales")
    f["accel"] = None if f["sales_ytd_yoy"] is None or f["sales_ytd_yoy_ly"] is None \
        else f["sales_ytd_yoy"] - f["sales_ytd_yoy_ly"]
    m_now, m_ly = _ratio(cum.get("op"), cum.get("sales")), _ratio(prior.get("op"), prior.get("sales"))
    f["margin"] = None if m_now is None else m_now * 100
    f["margin_delta"] = None if m_now is None or m_ly is None else (m_now - m_ly) * 100
    f["op_ytd_yoy"] = _pct(cum.get("op"), prior.get("op"))

    # ② 単独四半期のQoQ(前回までの短信が蓄積されている銘柄のみ)
    ym = _month(x["period_end"])
    sq = lambda ymx, fld, w="cum": single_quarter(idx, code, ymx, fld, w)  # noqa: E731
    f["sales_q"], f["op_q"] = sq(ym, "sales"), sq(ym, "op")
    prev_sales, prev_op = sq(_shift(ym, -3), "sales"), sq(_shift(ym, -3), "op")
    f["sales_qoq"] = _pct(f["sales_q"], prev_sales)
    if prev_op is not None and f["op_q"] is not None and prev_op <= 0 < f["op_q"]:
        f["op_qoq"] = 999.0                    # 赤字→黒字転換は急改善として扱う
    else:
        f["op_qoq"] = _pct(f["op_q"], prev_op)
    f["sales_qoq_ly"] = _pct(sq(ym, "sales", "prior"), sq(_shift(ym, -3), "sales", "prior"))

    # ③B と進捗率(累計営業益 ÷ 通期予想。標準は 1Q 25% / 2Q 50% / 3Q 75%)
    f["conservatism"] = conservatism(x)
    fc_op = x["forecast"].get("op") if x["n_q"] < 4 else None
    f["progress"] = None if fc_op is None or fc_op <= 0 or cum.get("op") is None else cum["op"] / fc_op * 100

    rc, kc, bc = tcfg["recruit"], tcfg["kioxia"], tcfg["rorze_b"]
    f["recruit"] = bool(f["accel"] is not None and f["accel"] >= rc["min_accel"]
                        and f["margin_delta"] is not None and f["margin_delta"] >= rc["min_margin_delta"])
    # 季節性: 前年の同じQoQより min_qoq_excess pt 以上強いこと(前年データが無ければ問わない)
    seasonal_ok = f["sales_qoq"] is not None and (
        f["sales_qoq_ly"] is None or f["sales_qoq"] - f["sales_qoq_ly"] >= kc["min_qoq_excess"])
    f["kioxia"] = bool(f["sales_qoq"] is not None and f["sales_qoq"] >= kc["min_sales_qoq"]
                       and f["op_qoq"] is not None and f["op_qoq"] >= kc["min_op_qoq"] and seasonal_ok)
    f["rorze_b"] = bool(x.get("revised") is False and f["op_ytd_yoy"] is not None and f["op_ytd_yoy"] > 0
                        and f["conservatism"] is not None and f["conservatism"] <= bc["max_conservatism"])
    f["types"] = "".join(m for k, m in TYPE_MARKS.items() if f[k])
    return f


def evaluate(code: str, disclosed_at: str, x: dict, idx: dict, tcfg: dict) -> dict:
    row = {"code": code, "disclosed_at": disclosed_at, "n_q": x.get("n_q", ""), "period_end": x.get("period_end", ""),
           "revised": "" if x.get("revised") is None else str(x["revised"])}
    if not x:
        return {**row, "status": "no_xbrl"}
    if x["cum"].get("sales") is None and x["cum"].get("op") is None:
        return {**row, "status": "no_data"}
    f = compute(x, idx, code, tcfg)
    return {**row, **{k: (round(v, 2) if isinstance(v, float) else v) for k, v in f.items()}, "status": "ok"}


# ---------------------------------------------------------------- 取り込み

def update(cfg: dict, data_dir: Path = DATA_DIR, tdnet_session: RateLimitedSession | None = None,
           limit: int | None = None) -> dict:
    """未評価の決算短信(全市場)を TDnet の XBRL で評価し、短信の数値を tdnet_earnings.csv に蓄積する。"""
    ecfg = cfg["earnings"]
    tdnet_session = tdnet_session or RateLimitedSession.from_config(cfg)

    disc = disclosures.load(data_dir)
    feats = load(data_dir)
    arch = load_archive(data_dir)
    done = set(feats.loc[feats["status"].isin(["ok", "no_xbrl", "no_data"]), "disclosure_id"])
    todo = disc[(disc["kind"] == "earnings_report") & (disc["is_correction"] != "True")
                & ~disc["disclosure_id"].isin(done)]
    todo = todo.sort_values("disclosed_at").head(limit or ecfg["max_per_run"])   # 古い順: 前の短信を先に蓄積

    arch_rows = arch.to_dict("records")
    idx = archive_index(arch_rows)
    rows, new_arch = [], []
    for r in todo.itertuples():
        try:
            x = tdnet.fetch_earnings(tdnet_session, r.xbrl_url) if r.xbrl_url else {}
        except Exception as e:                       # 1件の失敗で止めない。次回再試行
            log.warning("決算評価失敗 %s %s: %s", r.code, r.disclosure_id, e)
            continue
        if x and x.get("period_end"):
            a = archive_row(r.disclosure_id, r.code, r.disclosed_at, x)
            new_arch.append(a)
            idx[(r.code, _month(a["period_end"]))] = a
        rows.append({"disclosure_id": r.disclosure_id, **evaluate(r.code, r.disclosed_at, x, idx, ecfg["types"]),
                     "computed_at": now_jst().strftime("%Y-%m-%dT%H:%M")})

    if rows:
        new = pd.DataFrame(rows).reindex(columns=FEATURE_COLUMNS, fill_value="")
        feats = pd.concat([feats[~feats["disclosure_id"].isin(new["disclosure_id"])], new])
        feats.sort_values(["disclosed_at", "disclosure_id"], ascending=False).to_csv(
            features_path(data_dir), index=False, encoding="utf-8")
    if new_arch:
        append_history(new_arch, data_dir)
        na = pd.DataFrame(new_arch).reindex(columns=ARCHIVE_COLUMNS)
        arch = pd.concat([arch[~arch["disclosure_id"].isin(na["disclosure_id"])], na])
        arch.sort_values(["code", "period_end", "disclosed_at"]).to_csv(archive_path(data_dir), index=False,
                                                                       encoding="utf-8")
    stats = {"evaluated": len(rows), "archived": len(new_arch),
             "status": pd.Series([r["status"] for r in rows]).value_counts().to_dict() if rows else {}}
    log.info("earnings: %s", stats)
    return stats


def append_history(arch_rows: list[dict], data_dir: Path = DATA_DIR) -> int:
    """短信の累計から当四半期の単独値を出し、quarterly_history.csv に継ぎ足す(source=tdnet)。
    1Qは累計=単独。2Q以降は同じ期の前の四半期の単独値(履歴)を引く。"""
    if not history.path(data_dir).exists():
        return 0
    hist_df = history.load(data_dir)
    hist = history.by_code(hist_df)
    rows = []
    for a in sorted(arch_rows, key=lambda a: str(a["disclosed_at"])):
        n_q, ym, code = int(a["n_q"]), _month(a["period_end"]), a["code"]
        prev = [q for q in hist.get(code, []) if q["end"] in {_shift(ym, -3 * k) for k in range(1, n_q)}]
        if len(prev) != n_q - 1:
            continue
        cur = {"end": ym, "announced": str(a["disclosed_at"])[:10]}
        for f in FIELDS:
            c = _num(a.get(f"cum_{f}"))
            pv = [q.get(f) for q in prev]
            cur[f] = None if c is None or any(v is None for v in pv) else c - sum(pv)
        if cur["sales"] is None and cur["op"] is None:
            continue
        rows.append({"code": code, **cur, "source": "tdnet"})
        hist.setdefault(code, [])
        hist[code] = sorted([q for q in hist[code] if q["end"] != ym] + [cur], key=lambda q: q["end"])
    if rows:
        history.save(history.merge(hist_df, rows), data_dir)
    return len(rows)
