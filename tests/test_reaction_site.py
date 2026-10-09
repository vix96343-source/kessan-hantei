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
    (_row(kind="earnings_report"), "earnings"),
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
    (_row(direction="up"), "修正↑"),
    (_row(direction="down"), "修正↓"),
    (_row(), "修正"),
    (_row(kind="forecast_dividend_revision", direction="up", title="業績予想及び配当予想の修正（増配）"), "修正↑増配"),
    (_row(kind="dividend_revision", title="配当予想の修正（減配）"), "減配"),
])
def test_kind_label(row, label):
    assert site.kind_label(row) == label
