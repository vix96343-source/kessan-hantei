from datetime import date, timedelta

import pandas as pd
import pytest

from src import judge
from src.datasources import tdnet

FW = {
    "progress_gap": {"weight": 0.30, "scale_pt": 10, "years": 3, "min_years": 2},
    "guidance_acceleration": {"weight": 0.20, "scale_pt": 30},
    "guidance_conservatism": {"weight": 0.20, "scale_count": 2},
    "beat_history": {"weight": 0.0},
    "momentum_pricing": {"weight": -0.15, "ret_scale_pct": 20, "dev_scale_pct": 10},
    "revision_recency": {"weight": -0.10},
}
CFG = {"factors": FW, "judge": {
    "min_coverage": 0.5, "class_thresholds": {"gap2": 0.30, "gap1": 0.15, "caution": -0.15},
    "ev_per_score_pct": 10, "winrate_by_class": {"★★GAP": 62, "★GAP": 56, "PASS": 50, "CAUTION": 42}}}

B = 1e9
# MARUWA の単独四半期営業利益(2023/6〜2026/6、10億円)
MARUWA_OP = {"2023-06": 3.530, "2023-09": 5.119, "2023-12": 5.744, "2024-03": 5.408,
             "2024-06": 5.640, "2024-09": 7.149, "2024-12": 6.945, "2025-03": 7.180,
             "2025-06": 6.002, "2025-09": 4.841, "2025-12": 6.281, "2026-03": 7.852, "2026-06": 6.348}
HIST = [{"end": k, "op": v * B} for k, v in MARUWA_OP.items()]


def _snaps(rows):
    cols = ["disclosure_id", "code", "disclosed_at", "n_q", "period_end", "scope", "cum_sales", "cum_op",
            "prior_sales", "prior_op", "fc_sales", "fc_op", "fc_next_year"]
    return pd.DataFrame([{c: r.get(c, "") for c in cols} for r in rows])


ANNUAL = {"disclosure_id": "a", "code": "5344", "disclosed_at": "2026-05-08T10:40", "n_q": 4,
          "period_end": "2026-03-31", "cum_op": 24.976 * B, "prior_op": 26.914 * B, "fc_op": 29.7 * B, "fc_sales": 84.1 * B}
Q1 = {"disclosure_id": "b", "code": "5344", "disclosed_at": "2026-07-29T10:00", "n_q": 1, "period_end": "2026-06-30",
      "cum_op": 6.348 * B, "prior_op": 6.002 * B, "fc_op": 33.7 * B, "fc_sales": 93.3 * B}
Q2_AFTER = {"disclosure_id": "c", "code": "5344", "disclosed_at": "2026-10-27T15:30", "n_q": 2,
            "period_end": "2026-09-30", "cum_op": 17.605 * B, "prior_op": 10.843 * B, "fc_op": 38.3 * B}
NO_EVENTS = pd.DataFrame(columns=["disclosure_id", "code", "disclosed_at", "period", "cur_op", "prev_op",
                                  "cur_sales", "prev_sales"])


def test_state_before_uses_only_prior_disclosures_and_counts_upward_revision():
    st = judge.state_before("5344", date(2026, 10, 27), _snaps([ANNUAL, Q1, Q2_AFTER]), NO_EVENTS)
    assert st["n_q"] == 1 and st["fy_end"] == "2027-03"
    assert st["fc_op"] == 33.7 * B                    # 10/27 の短信(発表当日)は使わない
    assert (st["ups"], st["downs"]) == (1, 0)         # 期初 297億 → 1Q 337億


def test_state_before_applies_later_revision_event():
    ev = pd.DataFrame([{"disclosure_id": "r", "code": "5344", "disclosed_at": "2026-09-15T15:00",
                        "period": "CurrentYearDuration", "cur_op": 30.0 * B, "prev_op": 33.7 * B,
                        "cur_sales": "", "prev_sales": ""}])
    st = judge.state_before("5344", date(2026, 10, 27), _snaps([ANNUAL, Q1]), ev)
    assert st["fc_op"] == 30.0 * B
    assert (st["ups"], st["downs"]) == (1, 1)


def test_progress_gap_maruwa():
    st = judge.state_before("5344", date(2026, 10, 27), _snaps([ANNUAL, Q1]), NO_EVENTS)
    p = judge.progress_gap(st, HIST, FW["progress_gap"])
    assert p["progress"] == 18.8
    assert p["past_avg"] == pytest.approx((3.530 / 19.801 + 5.640 / 26.914 + 6.002 / 24.976) / 3 * 100, abs=0.05)
    assert p["v"] == pytest.approx(p["gap_pt"] / 10, abs=0.01)


def test_guidance_acceleration_maruwa():
    st = judge.state_before("5344", date(2026, 10, 27), _snaps([ANNUAL, Q1]), NO_EVENTS)
    a = judge.guidance_acceleration(st, HIST, FW["guidance_acceleration"])
    # 残り期間の予想 33.7−6.348 = 27.352 vs 前年 2Q〜4Q 4.841+6.281+7.852 = 18.974 → +44.2%
    assert a["implied_yoy"] == 44.2
    assert a["ytd_yoy"] == 5.8
    assert a["v"] == 1.0                              # +38pt は上限


def test_guidance_acceleration_after_annual_report():
    st = judge.state_before("5344", date(2026, 7, 29), _snaps([ANNUAL]), NO_EVENTS)
    a = judge.guidance_acceleration(st, HIST, FW["guidance_acceleration"])
    assert a["implied_yoy"] == round((29.7 / 24.976 - 1) * 100, 1)
    assert a["ytd_yoy"] == round((24.976 / 26.914 - 1) * 100, 1)
    assert judge.progress_gap(st, HIST, FW["progress_gap"]) is None


def test_missing_history_returns_none():
    st = judge.state_before("5344", date(2026, 10, 27), _snaps([ANNUAL, Q1]), NO_EVENTS)
    assert judge.progress_gap(st, HIST[-2:], FW["progress_gap"]) is None
    # 前期の本決算短信があれば、四半期履歴が無くても「前期通期 − 前年同期累計」で計算できる
    a = judge.guidance_acceleration(st, [], FW["guidance_acceleration"])
    assert a["implied_yoy"] == 44.2
    st2 = judge.state_before("5344", date(2026, 10, 27), _snaps([Q1]), NO_EVENTS)
    assert judge.guidance_acceleration(st2, [], FW["guidance_acceleration"]) is None


def test_guidance_acceleration_skips_loss_making_ytd():
    loss = {**Q1, "cum_op": -1.0 * B}
    st = judge.state_before("5344", date(2026, 10, 27), _snaps([ANNUAL, loss]), NO_EVENTS)
    assert judge.guidance_acceleration(st, HIST, FW["guidance_acceleration"]) is None


def test_momentum_pricing_uses_only_closes_up_to_asof():
    d0 = date(2026, 9, 1)
    s = pd.Series([100.0] * 30 + [80.0] + [200.0], index=[d0 + timedelta(days=i) for i in range(32)])
    m = judge.momentum_pricing(s, d0 + timedelta(days=30), FW["momentum_pricing"])
    assert m["close"] == 80.0 and m["ret_20d"] == -20.0
    assert m["v"] == pytest.approx(0.5 * -1 + 0.5 * max(-1, (80 / ((100 * 24 + 80) / 25) - 1) * 100 / 10))
    assert judge.momentum_pricing(s.head(10), d0 + timedelta(days=30), FW["momentum_pricing"]) is None


@pytest.mark.parametrize("vals,cls", [
    ({"progress_gap": 1, "guidance_acceleration": 1}, "★★GAP"),
    ({"progress_gap": 0.6}, "★GAP"),
    ({"progress_gap": -0.6, "momentum_pricing": 0.2}, "CAUTION"),
    ({"progress_gap": 0.1, "guidance_conservatism": 0.1}, "PASS"),
])
def test_score_classes(vals, cls):
    f = {k: {"v": vals.get(k, 0.0)} for k in judge.FACTORS}
    assert judge.score(f, CFG)["gap_class"] == cls


def test_score_low_coverage_is_pass():
    f = {k: None for k in judge.FACTORS}
    f["guidance_acceleration"] = {"v": 1.0}
    s = judge.score(f, CFG)
    assert s["gap_class"] == "PASS" and s["conviction"] == "低"
    assert s["score_total"] == 0.2


def test_parse_earnings_facts_irbank():
    ctx = "CurrentAccumulatedQ1Duration_ConsolidatedMember_ResultMember"
    facts = [
        {"element_name": "NetSales", "context": ctx, "value": "19267000000", "unit": "JPY"},
        {"element_name": "OperatingIncome", "context": ctx, "value": "6348000000", "unit": "JPY"},
        {"element_name": "OperatingIncome", "context": ctx.replace("Current", "Prior", 1), "value": "6002000000", "unit": "JPY"},
        {"element_name": "OperatingIncome", "context": "CurrentYearDuration_ConsolidatedMember_ForecastMember",
         "value": "33700000000", "unit": "JPY"},
        {"element_name": "ChangeInOperatingIncome", "context": "CurrentYearDuration_ConsolidatedMember_ForecastMember",
         "value": 0.349, "unit": "Pure"},
        {"element_name": "CorrectionOfConsolidatedFinancialForecastInThisQuarter",
         "context": "CurrentYearDuration_ConsolidatedMember_ForecastMember", "value": "有", "unit": None},
        {"element_name": "CurrentPeriodEndDateDEI", "context": "FilingDateInstant", "value": "2026-06-30", "unit": None},
    ]
    x = tdnet.parse_earnings_facts(facts)
    assert (x["n_q"], x["period_end"], x["scope"]) == (1, "2026-06-30", "ConsolidatedMember")
    assert x["cum"]["op"] == 6348000000 and x["prior_cum"]["op"] == 6002000000
    assert x["forecast"]["op"] == 33700000000 and x["forecast_change"]["op"] == 34.9
    assert x["revised"] is True


def test_save_keeps_review_columns(tmp_path):
    row = {c: "" for c in judge.COLUMNS}
    first = pd.DataFrame([{**row, "run_date": "2026-10-26", "batch_id": "2026-10-26", "code": "5344",
                           "announce_date": "2026-10-27", "score_total": "0.1", "gap_open_pct": "14.9"}])
    judge.save(first, tmp_path)
    again = pd.DataFrame([{**row, "run_date": "2026-10-26", "batch_id": "2026-10-26", "code": "5344",
                           "announce_date": "2026-10-27", "score_total": "0.387"}])
    judge.save(again, tmp_path)
    df = judge.load(tmp_path)
    assert len(df) == 1
    assert df.iloc[0]["score_total"] == "0.387" and df.iloc[0]["gap_open_pct"] == "14.9"
