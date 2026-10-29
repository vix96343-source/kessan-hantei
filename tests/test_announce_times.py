import pandas as pd

from src import announce_times


def test_from_index_jst_and_excludes_corrections():
    idx = pd.DataFrame([
        {"code": "1878", "source": "tdnet", "disclosed_at": "2026-07-31T02:30:00.000Z",
         "title": "2027年３月期第１四半期決算短信〔日本基準〕(連結)"},
        {"code": "1878", "source": "tdnet", "disclosed_at": "2026-05-26T06:30:00.000Z",
         "title": "（訂正・数値データ訂正）「2026年３月期決算短信〔日本基準〕（連結）」の一部訂正に関するお知らせ"},
        {"code": "1878", "source": "tdnet", "disclosed_at": "2025-08-08T06:30:00.000Z",
         "title": "2026年3月期第1四半期決算短信〔日本基準〕(連結)(公認会計士等による期中レビューの完了)"},
        {"code": "1878", "source": "tdnet", "disclosed_at": "2026-07-31T02:30:00.000Z", "title": "剰余金の配当に関するお知らせ"},
    ])
    d = announce_times.from_index(idx)
    assert d.to_dict("records") == [{"code": "1878", "announce_date": "2026-07-31", "announce_time": "11:30"}]


def test_combine_keeps_first_time_per_day_and_latest_eight():
    rows = [{"code": "1941", "announce_date": f"20{y}-0{m}-28", "announce_time": "15:00"}
            for y in (24, 25, 26) for m in (1, 4, 7)]
    rows.append({"code": "1941", "announce_date": "2026-07-28", "announce_time": "14:00"})   # 同じ日の早い方
    d = announce_times.combine(pd.DataFrame(rows))
    assert len(d) == 8
    assert d.iloc[0].to_dict() == {"code": "1941", "announce_date": "2026-07-28", "announce_time": "14:00"}


def test_estimate_uses_last_time_and_counts():
    e = announce_times.estimate([("2026-07-29", "15:30"), ("2026-04-28", "15:40"), ("2026-01-29", "15:40"),
                                 ("2025-10-29", "15:30")])
    assert e == {"time": "15:30", "prev": "15:30", "last_date": "2026-07-29", "n": 4, "same": 2, "others": ["15:40×2"]}
    assert announce_times.estimate([]) is None


def test_estimate_uses_same_quarter_last_year_when_time_changes():
    # コクヨ: 10月(Q3)はいつも 16:00、ほかの四半期は 12:00
    hist = [("2026-07-31", "12:00"), ("2026-04-28", "16:00"), ("2026-02-13", "12:00"), ("2025-10-30", "16:00"),
            ("2025-07-30", "12:00")]
    e = announce_times.estimate(hist, on="2026-10-29")
    assert (e["time"], e["prev"], e["same"]) == ("16:00", "12:00", 2)
    # 前回と前々回が同じ会社は前回の時刻
    hist = [("2026-07-30", "15:00"), ("2026-04-30", "15:00"), ("2025-10-30", "13:00")]
    assert announce_times.estimate(hist, on="2026-10-29")["time"] == "15:00"
    # 今回の発表そのもの(on の日)は使わない
    assert announce_times.estimate([("2026-10-29", "11:40"), ("2026-07-28", "16:00")], on="2026-10-29")["last_date"] == "2026-07-28"


def test_from_disclosures_skips_corrections():
    disc = pd.DataFrame([
        {"code": "7419", "disclosed_at": "2026-10-29T08:30", "kind": "earnings_report", "is_correction": "False"},
        {"code": "7419", "disclosed_at": "2026-10-29T17:00", "kind": "earnings_report", "is_correction": "True"},
        {"code": "7419", "disclosed_at": "2026-10-29T08:30", "kind": "forecast_revision", "is_correction": "False"},
    ])
    d = announce_times.from_disclosures(disc)
    assert d.to_dict("records") == [{"code": "7419", "announce_date": "2026-10-29", "announce_time": "08:30"}]
