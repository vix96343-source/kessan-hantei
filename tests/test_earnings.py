import io
import zipfile

import pytest

from src import earnings
from src.datasources import kabutan, tdnet

TYPES = {
    "recruit": {"min_accel": 3.0, "min_margin_delta": 0.0},
    "kioxia": {"min_sales_qoq": 20.0, "min_op_qoq": 50.0, "min_qoq_excess": 10.0},
    "rorze_b": {"max_conservatism": 0.85},
}

KABUTAN_HTML = """
<div class="fin_quarter_t0_d fin_quarter_result_d"><table><tbody>
<tr><td colspan="8"><a href="#"></a></td></tr>
<tr><th scope="row">24.10-12&nbsp;</th><td>1,000</td><td>100</td><td>100</td><td>70</td><td>1</td><td>10</td>
<td><a href="#">25/01/30</a></td></tr>
<tr><th scope="row">25.11-01&nbsp;</th><td>1,200</td><td>－</td><td>130</td><td>80</td><td>1</td><td>10</td>
<td><a href="#">25/03/10</a></td></tr>
<tr><th scope="row">前年同期比</th><td>+1</td><td>+1</td><td>+1</td><td>+1</td><td>+1</td><td></td><td>(%)</td></tr>
</tbody></table></div>
"""


def test_parse_kabutan_quarterly():
    q = kabutan.parse_quarterly(KABUTAN_HTML)
    assert [x["period"] for x in q] == ["24.10-12", "25.11-01"]
    assert q[0]["end"] == "2024-12" and q[1]["end"] == "2026-01"     # 年をまたぐ四半期
    assert q[0]["sales"] == 1000e6 and q[1]["op"] is None
    assert q[0]["announced"] == "2025-01-30"


def _hist(ends_and_sales_op, announced_before="2026-10-01"):
    return [{"end": e, "sales": s, "op": o, "ordinary": o, "net": o, "announced": announced_before}
            for e, s, o in ends_and_sales_op]


# 3月決算・Q2(7-9月)を評価する。過去7四半期(25/3〜26/6)は売上100→ゆるやかに伸び、当Qで急加速
HIST = _hist([
    ("2024-12", 100, 10), ("2025-03", 100, 10), ("2025-06", 100, 10), ("2025-09", 100, 10),
    ("2025-12", 104, 10), ("2026-03", 104, 10), ("2026-06", 110, 12),
])


def _x(cum_sales, cum_op, n_q=2, fc_op=None, prior_op=None, revised=False, period_end="2026-09-30"):
    return {"n_q": n_q, "period_end": period_end, "scope": "ConsolidatedMember",
            "cum": {"sales": cum_sales, "op": cum_op, "ordinary": cum_op, "net": cum_op},
            "prior_cum": {"sales": 200, "op": prior_op if prior_op is not None else 20, "ordinary": 20, "net": 20},
            "forecast": {"sales": None, "op": fc_op, "ordinary": None, "net": None},
            "forecast_q2": {}, "forecast_is_next_year": n_q == 4, "revised": revised}


def test_quarter_series_derives_single_quarter_from_cumulative():
    s = earnings.quarter_series(HIST, _x(250, 32), "2026-10-30")
    assert s[-1] == {"end": "2026-09", "sales": 140, "op": 20, "ordinary": 20, "net": 20}   # 250 − 110


def test_quarter_series_rejects_gap_and_future_history():
    gap = [h for h in HIST if h["end"] != "2026-06"]
    assert earnings.quarter_series(gap, _x(250, 32), "2026-10-30") is None
    # 開示日以降に発表された履歴(当四半期そのもの)は使わない
    leaked = HIST + _hist([("2026-09", 140, 20)], announced_before="2026-10-30")
    assert earnings.quarter_series(leaked, _x(250, 32), "2026-10-30")[-1]["sales"] == 140


def test_types_recruit_and_kioxia():
    s = earnings.quarter_series(HIST, _x(250, 32), "2026-10-30")
    f = earnings.compute(s, _x(250, 32), TYPES)
    assert f["sales_q_yoy"] == pytest.approx(40.0)               # 140 / 100
    assert f["sales_ttm_yoy"] == pytest.approx((458 / 400 - 1) * 100)
    assert f["accel"] > 3 and f["margin_delta"] > 0
    assert f["sales_qoq"] == pytest.approx((140 / 110 - 1) * 100)   # 27.3%
    assert f["op_qoq"] == pytest.approx((20 / 12 - 1) * 100)        # 66.7%
    assert f["recruit"] and f["kioxia"]
    assert f["types"].startswith("①②")


def test_rorze_b_conservative_forecast():
    # 通期予想 50: 残り(Q3+Q4)の想定 = 50 − 32 = 18。前年の Q3+Q4 = 20、今期の伸び 32/20 = 1.6 → 18/32 = 0.56
    x = _x(250, 32, fc_op=50, prior_op=20, revised=False)
    s = earnings.quarter_series(HIST, x, "2026-10-30")
    f = earnings.compute(s, x, TYPES)
    assert f["conservatism"] == pytest.approx(18 / 32)
    assert f["rorze_b"]
    x_rev = {**x, "revised": True}
    assert not earnings.compute(s, x_rev, TYPES)["rorze_b"]         # 修正済みは対象外


def test_turnaround_counts_as_big_op_improvement():
    hist = HIST[:-1] + _hist([("2026-06", 110, -5)])
    s = earnings.quarter_series(hist, _x(250, 15), "2026-10-30")
    assert earnings.compute(s, _x(250, 15), TYPES)["op_qoq"] == 999.0


def test_evaluate_status():
    assert earnings.evaluate("1111", "2026-10-30T15:00", {}, HIST, TYPES)["status"] == "no_xbrl"
    assert earnings.evaluate("1111", "2026-10-30T15:00", _x(250, 32), [], TYPES)["status"] == "no_history"
    r = earnings.evaluate("1111", "2026-10-30T15:00", _x(250, 32), HIST, TYPES)
    assert r["status"] == "ok" and r["types"].startswith("①②")


def _ix(name, ctx, value, scale=6, sign=False):
    s = ' sign="-"' if sign else ""
    return f'<ix:nonFraction name="tse-ed-t:{name}" contextRef="{ctx}" scale="{scale}"{s}>{value}</ix:nonFraction>'


def test_parse_earnings_xbrl():
    cur, prior = "CurrentAccumulatedQ2Duration_ConsolidatedMember_ResultMember", "PriorAccumulatedQ2Duration_ConsolidatedMember_ResultMember"
    fy = "CurrentYearDuration_ConsolidatedMember_ForecastMember"
    body = "".join([
        f'<xbrli:context id="{cur}"><xbrli:period><xbrli:startDate>2026-04-01</xbrli:startDate>'
        f'<xbrli:endDate>2026-09-30</xbrli:endDate></xbrli:period></xbrli:context>',
        _ix("NetSales", cur, "7,670"), _ix("OperatingIncome", cur, "827"), _ix("OperatingIncome", prior, "864"),
        _ix("ChangeInOperatingIncome", cur, "4.3", scale=-2, sign=True),
        _ix("OperatingIncome", fy, "1,330"),
        _ix("ProfitAttributableToOwnersOfParent", cur, "237"), _ix("Profit", cur, "300"),
        '<ix:nonNumeric name="tse-ed-t:CorrectionOfConsolidatedFinancialForecastInThisQuarter" '
        f'contextRef="{fy}">無</ix:nonNumeric>',
    ])
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("XBRLData/Summary/tse-scedjpsy-75440-x-ixbrl.htm", f"<html>{body}</html>")
    x = tdnet.parse_earnings_xbrl(buf.getvalue())
    assert x["n_q"] == 2 and x["period_end"] == "2026-09-30"
    assert x["cum"]["sales"] == 7670e6 and x["cum"]["op"] == 827e6
    assert x["cum"]["net"] == 237e6                      # 親会社株主帰属を優先
    assert x["prior_cum"]["op"] == 864e6
    assert x["forecast"]["op"] == 1330e6
    assert x["revised"] is False
