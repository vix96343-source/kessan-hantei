from src import pres_orders


def test_reads_only_self_checking_rows():
    text = "\n".join(["（単位：百万円）", "2026年3月期 中間 2027年3月期 中間 増減額 増減率",
                      "受注高 28,362 26,856 ▲ 1,505 ▲ 5.3%", "受注残高 20,430 22,295 1,864 9.1%",
                      "売上高 1,000 1,100 100 10.0%"])
    # 同じ表の売上高が連結売上と合うときだけ
    hist = {"7505": {"2026-06": {"sales": 600e6}, "2026-03": {"sales": 500e6}}}
    x0 = pres_orders.parse_text(text, 2)
    assert pres_orders.matches_consolidated(x0, "7505", "2026-06", 2, hist)["orders"] == 26856e6
    assert pres_orders.matches_consolidated(x0, "7505", "2026-06", 2, {"7505": {"2026-06": {"sales": 900e6}, "2026-03": {"sales": 500e6}}}) is None
    x = pres_orders.parse_text(text, 2)
    assert (x["orders"], x["orders_yoy"], x["backlog"], x["backlog_yoy"]) == (26856e6, -5.3, 22295e6, 9.1)
    # 増減額が合わない行・単位の無い表・累計と分からない 2Q の表は読まない
    assert pres_orders.parse_text("（単位：百万円）\n受注高 100 120 30 20.0%\n売上高 1 2 1 100.0%", 1) is None
    assert pres_orders.parse_text("受注高 100 120 20 20.0%", 1) is None
    assert pres_orders.parse_text("（単位：百万円）\n受注高 100 120 20 20.0%", 2) is None
    # 単位が表のすぐ下
    assert pres_orders.parse_text("受注高 27,806 31,065 ＋3,259 111.7%\n売上高 20,000 22,000 2,000 10.0%\n（単位：百万円）", 1)["orders"] == 31065e6
