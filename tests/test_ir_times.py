from datetime import date

from bs4 import BeautifulSoup

from src import ir_times

D = date(2026, 10, 30)


def lines(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def test_table_row_with_time():
    html = "<table><tr><td>2026年10月30日(金)</td><td>2027年3月期 第2四半期決算発表(15:00予定)</td></tr></table>"
    r = ir_times.extract(lines(html), D)
    assert (r["status"], r["ir_time"]) == ("found", "15:00")


def test_dl_with_date_and_event_on_next_line():
    html = "<dl><dt>2026.10.30</dt><dd>第2四半期決算発表 13時30分</dd><dt>2026.11.5</dt><dd>決算説明会 16:00</dd></dl>"
    r = ir_times.extract(lines(html), D)
    assert r["ir_time"] == "13:30"


def test_briefing_time_is_not_taken():
    html = "<ul><li>10月30日 決算発表 / 決算説明会(電話会議)17:00〜</li></ul>"
    r = ir_times.extract(lines(html), D)
    assert (r["status"], r["ir_time"]) == ("no_time", "")


def test_briefing_only_line_is_skipped():
    html = "<ul><li>2026/10/30 決算説明会 16:00</li></ul>"
    assert ir_times.extract(lines(html), D) is None


def test_other_date_and_short_form_need_weekday():
    html = "<ul><li>2026/10/31 決算発表 15:00</li><li>10/30 株主優待発送 10:00</li></ul>"
    assert ir_times.extract(lines(html), D) is None
    r = ir_times.extract(lines("<p>10/30(金) 第2四半期決算短信 発表予定 午後3時30分</p>"), D)
    assert r["ir_time"] == "15:30"


def test_fullwidth_digits():
    r = ir_times.extract(lines("<p>２０２６年１０月３０日　決算発表（１５：３０）</p>"), D)
    assert r["ir_time"] == "15:30"


def test_title_first_items_keep_their_own_time():
    # 塩野義・ゼオン型: 項目ごとに「行事名 → 日時」。説明会の時刻を決算発表の時刻にしない
    html = ("<ul><li><p>2026年度R&D説明会</p><p>2026/10/30 13:00</p></li>"
            "<li><p>2027年3月期 第2四半期 決算発表</p><p>2026/10/30 15:30</p></li></ul>")
    r = ir_times.extract(lines(html), D)
    assert r["ir_time"] == "15:30"


def test_vague_hour_is_not_a_time():
    r = ir_times.extract(lines("<ul><li>2026年10月30日(金) 第2四半期決算発表(午前11時台)</li></ul>"), D)
    assert (r["status"], r["ir_time"]) == ("no_time", "")


def test_two_different_times_are_not_decided():
    r = ir_times.extract(lines("<p>2026年10月30日 決算発表 15:00 または 16:00</p>"), D)
    assert r["status"] == "no_time"
