from datetime import date

import pandas as pd
import pytest

from src import reaction, site

CLOSES = pd.Series([100.0, 110.0, 99.0, 120.0],
                   index=[date(2026, 10, 6), date(2026, 10, 7), date(2026, 10, 8), date(2026, 10, 9)])


def test_reaction_intraday_uses_same_day_close():
    assert reaction.reaction_of("2026-10-07T13:00", CLOSES, "15:30") == ("2026-10-06", "2026-10-07", 10.0)


def test_reaction_after_close_uses_next_day():
    assert reaction.reaction_of("2026-10-07T15:30", CLOSES, "15:30") == ("2026-10-07", "2026-10-08", -10.0)


def test_reaction_weekend_and_pending():
    s = CLOSES.copy()
    s.index = [date(2026, 10, 2), date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7)]
    assert reaction.reaction_of("2026-10-04T10:00", s, "15:30")[:2] == ("2026-10-02", "2026-10-05")
    assert reaction.reaction_of("2026-10-07T16:00", s, "15:30") is None    # 翌日終値まだ無し


def _row(**kw):
    return {"kind": "forecast_revision", "direction": "", "title": "", "change_pct": "", "is_correction": "False", **kw}


@pytest.mark.parametrize("row,b", [
    (_row(kind="earnings_report"), "earnings_nodata"),
    (_row(direction="up", change_pct="18.1"), "rev_up_large"),
    (_row(direction="down", change_pct="-3.0"), "rev_down_small"),
    (_row(direction="up"), "rev_up"),
    (_row(), "rev_unknown"),
    (_row(kind="dividend_revision", title="配当予想の修正（増配）"), "div_up"),
    (_row(kind="earnings_presentation"), None),
    (_row(direction="up", is_correction="True"), None),
])
def test_bucket(row, b):
    assert reaction.bucket(row, 10) == b


def test_earnings_bucket_by_type_count():
    feats = {"a": {"status": "ok", "types": ""}, "b": {"status": "ok", "types": "③"},
             "c": {"status": "ok", "types": "①②③"}, "d": {"status": "no_history", "types": ""}}
    got = [reaction.bucket({**_row(kind="earnings_report"), "disclosure_id": i}, 10, feats) for i in "abcd"]
    assert got == ["earnings_t0", "earnings_t1", "earnings_t2", "earnings_nodata"]


def test_probability_shrinks_to_base_rate():
    stats = {"base_rate": 0.4, "n": 100, "buckets": {"rev_up_large": {"n": 10, "wins": 9}}}
    assert reaction.probability("rev_up_large", stats, 10) == pytest.approx((9 + 4) / 20)
    assert reaction.probability("never_seen", stats, 10) == pytest.approx(0.4)
    assert reaction.probability(None, stats, 10) is None


def test_build_stats():
    disc = pd.DataFrame([
        {**_row(kind="earnings_report"), "disclosure_id": "a", "code": "1111"},
        {**_row(direction="up", change_pct="20"), "disclosure_id": "b", "code": "2222"},
        {**_row(kind="earnings_presentation"), "disclosure_id": "c", "code": "3333"},
    ])
    rx = pd.DataFrame({"disclosure_id": ["a", "b", "c"], "code": ["1111", "2222", "3333"],
                       "ref_date": [""] * 3, "out_date": [""] * 3, "ret_pct": [-1.0, 5.0, 3.0]})
    st = reaction.build_stats(disc, rx, 10)
    assert st["n"] == 2 and st["base_rate"] == 0.5
    assert st["buckets"]["rev_up_large"] == {"n": 1, "wins": 1, "mean_ret_pct": 5.0}


@pytest.mark.parametrize("row,label", [
    (_row(kind="earnings_report"), "決算"),
    ({**_row(kind="earnings_report"), "_feat": {"types": "①③"}}, "決算"),        # 型の印は種別に出さない
    (_row(direction="up"), "修正↑"),
    (_row(direction="down"), "修正↓"),
    (_row(), "修正"),
    (_row(kind="forecast_dividend_revision", direction="up", title="業績予想及び配当予想の修正（増配）"), "修正↑増配"),
    (_row(kind="dividend_revision", title="配当予想の修正（減配）"), "減配"),
])
def test_kind_label(row, label):
    assert site.kind_label(row, row.get("_feat")) == label


def test_details_for_earnings_and_revision():
    feat = {"status": "ok", "n_q": "2", "sales_ytd_yoy": "12.5", "op_ytd_yoy": "-3.0", "accel": "4.0",
            "margin_delta": "0.5", "progress": "62.0", "sales_qoq": "", "op_qoq": "", "conservatism": "0.7",
            "revised": "False"}
    d = {k: (v, t) for k, v, t in site.details(_row(kind="earnings_report"), feat)}
    assert d["売上(累計YoY)"] == ("+12.5%", "up")
    assert d["営業益(累計YoY)"] == ("-3.0%", "down")
    assert d["進捗率(営業益)"] == ("62%(標準50%)", "up")
    assert "売上(前四半期比)" not in d                     # 前の短信が無ければ出さない
    assert d["今回の予想修正"] == ("なし", "")

    rev = _row(direction="up", change_pct="18.1", period="CurrentYearDuration",
               xbrl_changes='{"NetSales": 2.0, "OperatingIncome": 18.1, "OrdinaryIncome": 15.0, "ProfitAttributableToOwnersOfParent": 30.2}')
    d = {k: v for k, v, _ in site.details(rev, None)}
    assert d == {"通期売上(修正率)": "+2.0%", "通期営業益(修正率)": "+18.1%", "通期経常益(修正率)": "+15.0%",
                 "通期純利益(修正率)": "+30.2%"}


@pytest.mark.parametrize("row,feat,label", [
    (_row(kind="earnings_report", title="2027年２月期 第２四半期（中間期）決算短信〔日本基準〕（連結）"), None, "2Q"),
    (_row(kind="earnings_report", title="2027年５月期  第１四半期決算短信〔日本基準〕（連結）"), None, "1Q"),
    (_row(kind="earnings_report", title="2026年11月期 第3四半期決算短信〔日本基準〕(連結)"), None, "3Q"),
    (_row(kind="earnings_report", title="2026年８月期 決算短信〔ＩＦＲＳ会計基準〕（連結）"), None, "通期"),
    (_row(kind="earnings_report", title="決算短信"), {"n_q": "2"}, "2Q"),                      # XBRL を優先
    (_row(title="業績予想の修正に関するお知らせ", period="CurrentAccumulatedQ2Duration"), None, "中間"),
    (_row(title="通期業績予想の修正に関するお知らせ"), None, "通期"),
    (_row(kind="dividend_revision", title="剰余金の配当（中間配当の増配）に関するお知らせ"), None, "中間"),
    (_row(kind="dividend_revision", title="配当予想の修正に関するお知らせ"), None, ""),
])
def test_period_label(row, feat, label):
    assert site.period_label(row, feat) == label


def test_pdf_links_attach_presentations_within_a_week():
    earn = {**_row(kind="earnings_report"), "code": "4735", "disclosed_at": "2026-10-09T16:00", "pdf_url": "tanshin.pdf"}
    pres = {"4735": [
        {"disclosed_at": "2026-10-09T16:00", "pdf_url": "setsumei.pdf", "title": "決算説明資料"},
        {"disclosed_at": "2026-10-14T15:00", "pdf_url": "hosoku.pdf", "title": "決算補足資料"},
        {"disclosed_at": "2026-10-30T15:00", "pdf_url": "next.pdf", "title": "別の資料"},      # 14日より後
        {"disclosed_at": "2026-10-08T15:00", "pdf_url": "before.pdf", "title": "前日の資料"},   # 短信より前
    ]}
    links = site.pdf_links(earn, pres)
    assert [(l["label"], l["url"]) for l in links] == [("短信", "tanshin.pdf"), ("資料", "setsumei.pdf"), ("資料2", "hosoku.pdf")]
    rev = {**_row(), "pdf_url": "shusei.pdf"}
    assert site.pdf_links(rev, pres) == [{"label": "修正", "url": "shusei.pdf"}]


def test_fin_panel_current_quarter_and_plan():
    hist = [{"end": "2026-03", "sales": 100e6, "op": 10e6, "ordinary": 10e6, "net": 7e6, "announced": "2026-04-10"},
            {"end": "2026-06", "sales": 120e6, "op": 12e6, "ordinary": 12e6, "net": 8e6, "announced": "2026-07-10"}]
    arch = {"n_q": "2", "period_end": "2026-09-30", "cum_sales": 250e6, "cum_op": 30e6, "cum_ordinary": 30e6,
            "cum_net": 20e6, "fc_sales": 500e6, "fc_op": 60e6, "fc_ordinary": 60e6, "fc_net": 40e6,
            "fc_next_year": "False"}
    f = site.fin_panel({"kind": "earnings_report"}, arch, hist, shares=1e6)
    assert [q["label"] for q in f["quarters"]] == ["26.01-03", "26.04-06", "26.07-09"]
    cur = f["quarters"][-1]
    assert (cur["sales"], cur["op"], cur["eps"]) == (130, 18, 12.0)          # 250 − 120、純利 20 − 8 = 12百万円 ÷ 100万株
    p = f["plan"]
    assert p["fy"] == "2027年3月期" and p["fc_label"] == "会社予想"
    assert p["rows"][0] == ["売上", 120, 250, None, None, 500, 50.0]           # 1Q累計, 2Q累計, 3Q, 通期, 予想, 進捗率


def test_quarter_numbers_single_quarter_yoy_qoq_and_eps():
    hist = [{"end": "2025-09", "sales": 100e6, "op": 10e6, "ordinary": 10e6, "net": 6e6, "announced": "2025-10-30"},
            {"end": "2026-03", "sales": 100e6, "op": 10e6, "ordinary": 10e6, "net": 6e6, "announced": "2026-05-10"},
            {"end": "2026-06", "sales": 110e6, "op": 11e6, "ordinary": 11e6, "net": 7e6, "announced": "2026-08-01"}]
    arch = {"n_q": "2", "period_end": "2026-09-30", "cum_sales": 230e6, "cum_op": 25e6, "cum_ordinary": 25e6,
            "cum_net": 16e6, "prior_sales": 200e6, "prior_op": 20e6, "prior_ordinary": 20e6, "prior_net": 12e6,
            "cum_eps": 32.0, "prior_eps": 24.0}
    n = site.quarter_numbers(arch, hist)
    # 2Q の3か月 = 累計230 − 1Q 110 = 120。前年2Q(2025-09)100 → +20%、前四半期110 → +9.1%
    assert n["sales"] == {"v": 120, "yoy": 20.0, "qoq": 9.1}
    # EPS(3か月) = 純利(16 − 7 = 9百万円) ÷ 株数(16百万円 ÷ 32円 = 50万株) = 18円。前年2Q 6百万円 ÷ 50万株 = 12円、前四半期 14円
    assert n["eps"] == {"v": 18.0, "yoy": 50.0, "qoq": 28.6}
    # 前の四半期が無ければ短信どおりの累計
    assert site.quarter_numbers(arch, [])["sales"] == {"v": 230, "yoy": 15.0, "qoq": None, "cum": True}
