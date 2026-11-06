from src import consensus, site

# Yahoo!ファイナンスの業績ページに埋め込まれている「業績予想(連結・確定)」の形(エスケープされた JSON)
HTML = ('...ConsensusContainer__updateDate\\",\\"dateTime\\":\\"2026-11-05T13:07:33+09:00\\"...'
        '{\\"settlementYm\\":\\"202703\\",\\"totalSampleCount\\":14,\\"netSales\\":53990285.71,'
        '\\"operatingIncome\\":3842242.86,\\"ordinaryIncome\\":5255242.86,\\"netProfit\\":3770185.71},'
        '{\\"settlementYm\\":\\"202803\\"}]...')


def test_parse_yj_reads_annual_consensus():
    x = consensus.parse_yj(HTML)
    assert x == {"yj_ym_0y": "202703", "yj_n_0y": 14, "yj_sales_0y": 53990285.7, "yj_op_0y": 3842242.9,
                 "yj_ord_0y": 5255242.9, "yj_net_0y": 3770185.7, "yj_updated": "2026-11-05"}
    assert consensus.parse_yj("<html>予想なし</html>") == {}


def test_panel_shows_operating_profit_for_this_fiscal_year():
    snap = {**consensus.parse_yj(HTML), "fetched_at": "2026-11-04T20:05", "before": True,
            "eps_0y": 250.5, "rev_0q": 13.5e12, "n_0y": 19}
    p = site.consensus_panel(snap, {"n_q": "2", "period_end": "2026-09-30"})   # 3月期の2Q → 今期 = 2027/03
    rows = {r["label"]: r["vals"] for r in p["rows"]}
    assert p["cols"] == ["今期2Q", "今期3Q", "今期通期", "来期通期"]
    assert rows["営業利益"] == [None, None, 3842243, None]
    assert rows["売上"] == [13500000, None, 53990286, None]
    assert rows["EPS"] == [None, None, 250.5, None]
    assert p["n"] == 14
