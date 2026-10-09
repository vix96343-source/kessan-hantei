import io
from datetime import date

import pandas as pd

from src import calendar_fetch, universe
from src.bizdays import add_business_days, is_business_day, next_business_day
from src.datasources import jpx


def test_business_days():
    assert not is_business_day(date(2026, 10, 12))          # スポーツの日
    assert next_business_day(date(2026, 10, 9)) == date(2026, 10, 13)
    assert next_business_day(date(2026, 12, 30)) == date(2027, 1, 4)   # 年末年始休場
    assert add_business_days(date(2026, 10, 9), 5) == date(2026, 10, 19)
    assert add_business_days(date(2026, 10, 13), -1) == date(2026, 10, 9)


def _xlsx(df: pd.DataFrame, header=True) -> bytes:
    buf = io.BytesIO()
    df.to_excel(buf, index=False, header=header)
    return buf.getvalue()


def test_parse_listed():
    src = pd.DataFrame({
        "日付": ["20260930"] * 4, "コード": ["7203", "130A", "1306", "9999"],
        "銘柄名": ["トヨタ", "グロース社", "ETF", "外国株"],
        "市場・商品区分": ["プライム（内国株式）", "グロース（内国株式）", "ETF・ETN", "スタンダード（外国株式）"],
        "33業種コード": ["3700", "5250", "-", "3050"], "33業種区分": ["輸送用機器", "情報・通信業", "-", "食料品"],
        "17業種コード": ["6"] * 4, "17業種区分": ["x"] * 4, "規模コード": ["1"] * 4, "規模区分": ["x"] * 4,
    })
    listed = jpx.parse_listed(_xlsx(src))
    assert listed["market"].tolist() == ["プライム", "グロース", "", "スタンダード"]
    u = universe.build(listed, {"7203": 5e10}, "2026-10-09T18:30")
    assert u["code"].tolist() == ["7203"]           # グロース・ETF・外国株式は除外
    assert u.iloc[0]["avg_turnover_20d"] == 5e10


def _schedule_xlsx() -> bytes:
    rows = [
        ["9月に四半期末又は期末を迎えた会社の一覧"] + [None] * 10,
        ["As of 2026/10/1"] + [None] * 10,
        ["決算発表予定日\nScheduled Dates for Earnings Announcements", "コード\nCode", "会社名", "Issue Name",
         "決算期末", "業種名", "Industry", "種別", "Fiscal Year/Quarter", "市場区分", "Market Segment"],
        ["2026-10-13", "2753", "あみやき亭", "x", "2027-03-31", "小売業", "x", "第２四半期", "x", "プライム", "x"],
        ["2026-10-15", "130A", "英字社", "x", "2027-03-31", "情報", "x", "本決算", "x", "スタンダード", "x"],
        ["未定_Undecided", "1111", "未定社", "x", "2027-03-31", "情報", "x", "第２四半期", "x", "プライム", "x"],
        ["2026-10-30", "2222", "遠い社", "x", "2027-03-31", "情報", "x", "第２四半期", "x", "プライム", "x"],
        ["※ テクニカル上場した会社は…"] + [None] * 10,
    ]
    return _xlsx(pd.DataFrame(rows), header=False)


def test_parse_schedule():
    s = jpx.parse_schedule(_schedule_xlsx())
    assert s["code"].tolist() == ["2753", "130A", "2222"]      # 未定・注記行は除外
    assert s["fiscal_q"].tolist() == ["Q2", "FY", "Q2"]
    assert s["announce_date"].iloc[0] == date(2026, 10, 13)


def test_calendar_build_horizon_and_actual_time():
    s = jpx.parse_schedule(_schedule_xlsx())
    disc = pd.DataFrame([{"code": "2753", "kind": "earnings_report", "disclosed_at": "2026-10-13T13:00"}])
    cal = calendar_fetch.build(s, date(2026, 10, 9), 5, "15:00", disc, "2026-10-09T18:30")
    assert cal["code"].tolist() == ["2753", "130A"]           # 10/30 は5営業日より先
    a, b = cal.iloc[0], cal.iloc[1]
    assert (a["announce_time"], a["announce_timing"], a["time_confirmed"]) == ("13:00", "場中", "true")
    assert (b["announce_time"], b["announce_timing"], b["time_confirmed"]) == ("", "引け後", "false")


def test_universe_keeps_previous_turnover_and_drops_5digit_codes():
    listed = pd.DataFrame({
        "code": ["7203", "4617", "92025"], "name": ["a", "b", "c"], "market": ["プライム"] * 3,
        "market_raw": ["プライム（内国株式）"] * 3, "sector33": ["x"] * 3,
    })
    u = universe.build(listed, {"7203": 2e10}, "t", previous={"7203": 1e10, "4617": 3e8})
    assert u["code"].tolist() == ["4617", "7203"]
    assert u.set_index("code")["avg_turnover_20d"].to_dict() == {"4617": 3e8, "7203": 2e10}
