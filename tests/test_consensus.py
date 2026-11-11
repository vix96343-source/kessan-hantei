from src import consensus, site

# 株予報「アナリスト予想」の決算期の表(予 = 会社予想、コ = コンセンサス)と日付
ANALYST = """<table><tr><th>日付</th><td>2026/11/05</td><td>1週間前</td></tr></table>
<table><tr><th>決算期</th><th>売上高</th><th>営業利益</th><th>経常利益</th><th>当期利益</th></tr>
<tr><td>202603</td><td>139,364 （6.3%)</td><td>17,437 （13.4%)</td><td>17,840 （8.2%)</td><td>10,995 （-19.9%)</td></tr>
<tr><td>202703
        予</td><td>160,000 （14.8%)</td><td>20,000 （14.7%)</td><td>20,500 （14.9%)</td><td>13,000 （18.2%)</td></tr>
<tr><td>202703
        コ</td><td>148,500 （6.6%)</td><td>18,300 （4.9%)</td><td>19,200 （7.6%)</td><td>13,100 （19.1%)</td></tr></table>"""
# 株予報「業績」の経常利益の四半期累計
REPORT = """<table><tr><th></th><th>第1四半期</th><th>中間</th><th>第3四半期</th><th>通期</th></tr>
<tr><td>会社予想</td><td>--</td><td>--</td><td>--</td><td>(10/30) 20,500</td></tr>
<tr><td>コンセンサス予想</td><td>4,100</td><td>9,000</td><td>14,000</td><td>19,200</td></tr></table>"""


def test_parse_kabuyoho_consensus_rows():
    assert consensus.parse_kb_analyst(ANALYST) == {
        "kb_ym_0y": "202703", "kb_sales_0y": 148500.0, "kb_op_0y": 18300.0, "kb_ord_0y": 19200.0,
        "kb_net_0y": 13100.0, "kb_date": "2026-11-05"}
    assert consensus.parse_kb_analyst(ANALYST.replace("コ</td>", "</td>")) == {}      # 予想の無い会社
    assert consensus.parse_kb_report(REPORT) == {"kb_ord_q1": 4100.0, "kb_ord_q2": 9000.0, "kb_ord_q3": 14000.0}


def test_panel_shows_kabuyoho_and_eps_from_shares():
    snap = {**consensus.parse_kb_analyst(ANALYST), **consensus.parse_kb_report(REPORT),
            "fetched_at": "2026-11-05T20:05", "before": True}
    p = site.consensus_panel(snap, {"n_q": "2", "period_end": "2026-09-30"}, shares=50_000_000)
    rows = {r["label"]: r["vals"] for r in p["rows"]}
    assert p["cols"] == ["コンセ(中間累計)", "今期通期コンセ"]                       # 空の列は出さない
    assert rows["営業利益"] == [None, 18300]
    assert rows["経常利益"] == [9000, 19200]
    assert rows["EPS"] == [None, 262.0]                                          # 13,100百万円 ÷ 5千万株
    # 実績・会社予想があれば並べて差と進捗率
    arch = {"n_q": "2", "period_end": "2026-09-30", "cum_sales": 70e9, "cum_op": 9e9, "cum_ordinary": 9.9e9,
            "cum_net": 6e9, "fc_sales": 150e9, "fc_op": 18e9, "fc_ordinary": 19e9, "fc_net": 13e9, "fc_next_year": "False"}
    p = site.consensus_panel(snap, arch, shares=50_000_000)
    rows = {r["label"]: r["vals"] for r in p["rows"]}
    assert p["cols"] == ["実績(中間累計)", "コンセ(中間累計)", "差%", "今期通期コンセ", "会社予想", "進捗率%"]
    assert rows["経常利益"] == [9900, 9000, 10.0, 19200, 19000, 51.6]
    assert p["pct"] == [2, 5]
    assert (p["src"], p["date"]) == ("株予報", "2026-11-05")


def test_snapshot_prefers_kabuyoho_even_after_the_announcement():
    yf = {"fetched_at": "2026-10-20T20:05", "rev_0q": 1e9, "kb_ym_0y": ""}
    kb1 = {"fetched_at": "2026-11-06T12:30", "kb_ym_0y": "202703"}
    kb2 = {"fetched_at": "2026-11-07T20:05", "kb_ym_0y": "202703"}
    s = consensus.snapshot([yf, kb1, kb2], "2026-10-30T15:30")      # 発表後に初めて株予報を取った会社
    assert s["fetched_at"] == "2026-11-06T12:30" and s["before"] is False
    s = consensus.snapshot([yf, kb1, kb2], "2026-11-07T15:00")
    assert s["fetched_at"] == "2026-11-06T12:30" and s["before"] is True
    assert consensus.snapshot([yf], "2026-10-30T15:30")["fetched_at"] == "2026-10-20T20:05"
