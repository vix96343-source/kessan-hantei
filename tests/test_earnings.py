import io
import zipfile

import pandas as pd
import pytest

from src import disclosures, earnings
from src.datasources import tdnet

TYPES = {
    "recruit": {"min_accel": 3.0, "min_margin_delta": 0.0},
    "kioxia": {"min_sales_qoq": 20.0, "min_op_qoq": 50.0, "min_qoq_excess": 10.0},
    "rorze_b": {"max_conservatism": 0.85},
}


def _x(n_q, period_end, cum, prior, prior_change=None, fc=None, fc_change=None, revised=False):
    """cum / prior = (sales, op)"""
    return {"n_q": n_q, "period_end": period_end, "scope": "ConsolidatedMember",
            "cum": {"sales": cum[0], "op": cum[1], "ordinary": None, "net": None},
            "prior_cum": {"sales": prior[0], "op": prior[1], "ordinary": None, "net": None},
            "prior_change": {"sales": prior_change, "op": None, "ordinary": None, "net": None},
            "forecast": {"sales": None, "op": fc, "ordinary": None, "net": None},
            "forecast_change": {"sales": None, "op": fc_change, "ordinary": None, "net": None},
            "forecast_q2": {}, "forecast_q2_change": {}, "forecast_is_next_year": n_q == 4, "revised": revised}


def test_recruit_uses_ytd_growth_vs_last_years_growth():
    # 今期累計 売上 +20%(去年は +5%)→ 加速 15pt、営業利益率 10% → 15%
    x = _x(2, "2026-09-30", (240, 36), (200, 20), prior_change=5.0)
    f = earnings.compute(x, {}, "1111", TYPES)
    assert f["sales_ytd_yoy"] == pytest.approx(20.0)
    assert f["accel"] == pytest.approx(15.0)
    assert f["margin_delta"] == pytest.approx(5.0)
    assert f["recruit"] and f["types"] == "①"


def test_rorze_b_from_forecast_and_its_change_rate():
    # 通期予想 営業益 100(前期比 +25% → 前期実績 80)。2Q累計 60(前年 40 → 伸び 1.5)
    # 残り期間の想定 = 100 − 60 = 40、前年の残り = 80 − 40 = 40 → 慎重度 = 40 / (40 × 1.5) = 0.667
    x = _x(2, "2026-09-30", (500, 60), (450, 40), fc=100, fc_change=25.0, revised=False)
    f = earnings.compute(x, {}, "1111", TYPES)
    assert f["conservatism"] == pytest.approx(40 / 60)
    assert f["rorze_b"]
    assert not earnings.compute({**x, "revised": True}, {}, "1111", TYPES)["rorze_b"]   # 修正済みは対象外
    assert earnings.conservatism({**x, "n_q": 4}) is None                               # 本決算は対象外


def _arch(code, n_q, period_end, cum, prior, did=None):
    return {"disclosure_id": did or f"{code}-{period_end}", "code": code, "disclosed_at": period_end + "T15:00",
            "n_q": n_q, "period_end": period_end, "scope": "ConsolidatedMember",
            "cum_sales": cum[0], "cum_op": cum[1], "cum_ordinary": None, "cum_net": None,
            "prior_sales": prior[0], "prior_op": prior[1], "prior_ordinary": None, "prior_net": None}


def test_single_quarter_needs_previous_disclosure_in_same_year():
    idx = earnings.archive_index([
        _arch("1111", 1, "2026-06-30", (100, 10), (100, 10)),
        _arch("1111", 2, "2026-09-30", (230, 30), (205, 21)),
    ])
    assert earnings.single_quarter(idx, "1111", "2026-06", "sales") == 100       # 1Q は累計=単独
    assert earnings.single_quarter(idx, "1111", "2026-09", "sales") == 130       # 230 − 100
    assert earnings.single_quarter(idx, "1111", "2026-09", "sales", "prior") == 105
    assert earnings.single_quarter(idx, "1111", "2026-12", "sales") is None      # 無い四半期
    gap = earnings.archive_index([_arch("1111", 2, "2026-09-30", (230, 30), (205, 21))])
    assert earnings.single_quarter(gap, "1111", "2026-09", "sales") is None      # 前の短信が無い


def test_kioxia_qoq_with_seasonality_check():
    rows = [_arch("1111", 1, "2026-06-30", (100, 10), (100, 10)),
            _arch("1111", 2, "2026-09-30", (230, 30), (205, 21))]
    x = _x(2, "2026-09-30", (230, 30), (205, 21), prior_change=0.0)
    f = earnings.compute(x, earnings.archive_index(rows), "1111", TYPES)
    assert f["sales_qoq"] == pytest.approx(30.0)          # 130 / 100
    assert f["op_qoq"] == pytest.approx(100.0)            # 20 / 10
    assert f["sales_qoq_ly"] == pytest.approx(5.0)        # 前年: 105 / 100
    assert f["kioxia"]


def test_update_archives_and_uses_previous_disclosure(tmp_path, monkeypatch):
    (tmp_path / "universe.csv").write_text("code,name,market,sector33,avg_turnover_20d,updated_at\n"
                                           "1111,テスト,プライム,x,1e9,t\n", encoding="utf-8")
    disc = [
        {"disclosure_id": "q2", "disclosed_at": "2026-10-30T15:00", "code": "1111", "kind": "earnings_report",
         "is_correction": "False", "xbrl_url": "q2.zip"},
        {"disclosure_id": "q1", "disclosed_at": "2026-07-30T15:00", "code": "1111", "kind": "earnings_report",
         "is_correction": "False", "xbrl_url": "q1.zip"},
    ]
    pd.DataFrame(disc).reindex(columns=disclosures.COLUMNS, fill_value="").to_csv(tmp_path / "disclosures.csv",
                                                                                index=False)
    xs = {"q1.zip": _x(1, "2026-06-30", (100, 10), (100, 10), prior_change=0.0),
          "q2.zip": _x(2, "2026-09-30", (230, 30), (205, 21), prior_change=0.0)}
    monkeypatch.setattr(earnings.tdnet, "fetch_earnings", lambda s, url: xs[url])
    st = earnings.update({"earnings": {"max_per_run": 10, "types": TYPES}}, data_dir=tmp_path, tdnet_session=object())
    assert st == {"evaluated": 2, "archived": 2, "status": {"ok": 2}}
    f = earnings.load(tmp_path).set_index("disclosure_id")
    assert float(f.loc["q2", "sales_qoq"]) == 30.0                # 古い順に処理して 1Q の短信を使えた
    assert len(earnings.load_archive(tmp_path)) == 2
    assert earnings.update({"earnings": {"max_per_run": 10, "types": TYPES}}, data_dir=tmp_path,
                           tdnet_session=object())["evaluated"] == 0


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
        _ix("NetSales", prior, "7,613"), _ix("ChangeInNetSales", prior, "8.7", scale=-2),
        _ix("ChangeInOperatingIncome", cur, "4.3", scale=-2, sign=True),
        _ix("OperatingIncome", fy, "1,330"), _ix("ChangeInOperatingIncome", fy, "6.0", scale=-2, sign=True),
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
    assert x["prior_change"]["sales"] == pytest.approx(8.7)
    assert x["forecast"]["op"] == 1330e6 and x["forecast_change"]["op"] == pytest.approx(-6.0)
    assert x["revised"] is False
