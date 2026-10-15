from src import segments
from src.site import segment_numbers

# ローツェ 1Q の短信 PDF のセグメント情報(前年同期の表 → 当期の表の順)。見出しが折り返して「(△)」が数値の行に残る
PDF = """
前第１四半期連結累計期間(自 2025年３月１日 至 2025年５月31日)
計 32,976 110 33,086 △25 33,061
セグメント利益又は損失
(△) 8,786 △110 8,675 △234 8,441
当第１四半期連結累計期間(自 2026年３月１日 至 2026年５月31日)
外部顧客への売上高 37,075 131 37,206 － 37,206
計 37,106 131 37,237 △30 37,206
セグメント利益又は損失
(△) 10,834 △119 10,714 △484 10,230
"""
TPL = {"cols": ["半導体・ＦＰＤ関連装置事業", "ライフサイエンス事業", None, None, None], "n": 5, "unit": 1e6}


def test_previous_quarter_segments_from_pdf():
    x = segments.parse_pdf_text(PDF, TPL)
    semi, life = x["segments"]
    # 売上は外部顧客への売上高(当期の表にある 37,075)。前年同期の表には無いので「計」の 32,976
    assert (semi["sales"], semi["op"], semi["sales_prior"], semi["op_prior"]) == (37075e6, 10834e6, 32976e6, 8786e6)
    assert (life["sales"], life["op"]) == (131e6, -119e6)


def _rec(n_q, end, sales, op, sales_prior, op_prior, at):
    return {"disclosure_id": at, "disclosed_at": at, "n_q": str(n_q), "period_end": end,
            "data": {"segments": [{"name": "半導体・ＦＰＤ関連装置事業", "sales": sales * 1e6, "op": op * 1e6,
                                   "sales_prior": sales_prior * 1e6, "op_prior": op_prior * 1e6},
                                  {"name": "ライフサイエンス事業", "sales": 1e6, "op": 0.0, "sales_prior": 1e6, "op_prior": 0.0}],
                     "total": None}}


def test_segment_values_are_as_in_tanshin_with_qoq_of_three_months():
    q1 = _rec(1, "2026-05", 37106, 10834, 32976, 8786, "2026-07-08")
    q2 = _rec(2, "2026-08", 79388, 23969, 66000, 17000, "2026-10-08")      # 2Q 累計
    g = segment_numbers(q2, [q1], None)
    semi = g["segments"][0]
    assert semi["sales"]["v"] == 79388000                                  # 短信の値(累計)そのまま、千円
    assert semi["sales"]["yoy"] == round((79388 - 66000) / 66000 * 100, 1) # 短信の前年同期比
    assert semi["sales"]["qoq"] == round((42282 - 37106) / 37106 * 100, 1) # 3か月どうし
    assert semi["op"]["v"] == 23969000
    # 前の四半期の記録が無ければ QoQ だけ空
    s0 = segment_numbers(q2, [], None)["segments"][0]["sales"]
    assert (s0["v"], s0["qoq"]) == (79388000, None)


def test_thousand_yen_segments_to_the_last_digit():
    """千円の短信は千円のまま一の位まで。合計はセグメントを足した「計」"""
    def rec(n_q, end, a, b, at):
        return {"disclosure_id": at, "disclosed_at": at, "n_q": str(n_q), "period_end": end,
                "data": {"segments": [{"name": "A", "sales": a * 1e3, "op": 1e3, "sales_prior": None, "op_prior": None},
                                      {"name": "B", "sales": b * 1e3, "op": 2e3, "sales_prior": None, "op_prior": None}],
                         "tpl": {"cols": ["A", "B"], "n": 2, "unit": 1000}}}
    q1 = rec(1, "2026-05", 2088387, 868197, "2026-07-14")
    q2 = rec(2, "2026-08", 4242100, 1565437, "2026-10-15")
    g = segment_numbers(q2, [q1], {"sales": {"v": 10975}, "op": {"v": 764}})
    assert g["unit"] == "千円"
    assert [s["sales"]["v"] for s in g["segments"]] == [4242100, 1565437]   # 短信の千円の値そのまま
    assert g["total"] is None                                             # 短信の表の合計が無ければ合計の行は出さない(百万円の丸めた値は出さない)
    assert "raw" not in g["segments"][0]["sales"]


def test_total_is_the_tanshin_value_not_a_sum():
    """合計は短信の表の値そのまま(セグメントの端数で、足すと 3,306,021 になるが短信は 3,306,022)"""
    def rec(n_q, end, a, b, tot, at):
        return {"disclosure_id": at, "disclosed_at": at, "n_q": str(n_q), "period_end": end,
                "data": {"segments": [{"name": "A", "sales": a * 1e3, "op": 1e3, "sales_prior": None, "op_prior": None},
                                      {"name": "B", "sales": b * 1e3, "op": 2e3, "sales_prior": None, "op_prior": None}],
                         "total": {"name": "合計", "sales": tot * 1e3, "op": 3e3, "sales_prior": 3000000e3, "op_prior": None},
                         "tpl": {"cols": ["A", "B"], "n": 3, "unit": 1000}}}
    q1 = rec(1, "2026-05", 2000000, 1000000, 3000001, "2026-07-14")
    q2 = rec(2, "2026-08", 2250022, 1056000, 3306022, "2026-10-15")
    g = segment_numbers(q2, [q1], None)
    assert g["total"]["sales"]["v"] == 3306022
    assert g["total"]["sales"]["q"] == 3306022 - 3000001
    assert g["total"]["sales"]["yoy"] == round((3306022 - 3000000) / 3000000 * 100, 1)


def test_pdf_total_is_the_last_column():
    x = segments.parse_pdf_text(PDF, TPL)
    assert (x["total"]["sales"], x["total"]["op"]) == (37206e6, 10230e6)
    assert (x["total"]["sales_prior"], x["total"]["op_prior"]) == (33061e6, 8441e6)


def test_sen_truncates_below_thousand_yen():
    from src.site import _sen
    assert (_sen(4242100999.0), _sen(-41525999.0), _sen(1e3 * 4242100), _sen(None)) == (4242100, -41525, 4242100, None)


def test_pdf_external_sales_label_wrapped():
    # 吉野家 1Q: 「外 部 顧 客 へ の」が前の行に折り返し、数値の行は「売上高」
    text = """報告セグメント
外 部 顧 客 へ の
売上高 38,779 8,782 7,797 55,359 3,411 58,771 ― 58,771
セ グ メ ン ト 間
又は振替高
285 51 ― 336 372 709 △709 ―
計 39,064 8,834 7,797 55,696 3,784 59,480 △709 58,771
セグメント利益 2,498 861 524 3,883 142 4,025 △1,480 2,544
"""
    tpl = {"cols": ["吉野家", "はなまる", "海外", None, "その他", None, None, None], "n": 8, "unit": 1e6}
    x = segments.parse_pdf_text(text, tpl)
    assert [s["sales"] for s in x["segments"]] == [38779e6, 8782e6, 7797e6, 3411e6]
    assert x["total"]["sales"] == 58771e6 and x["total"]["op"] == 2544e6


def test_segment_table_without_xbrl_tags():
    # 放電精密: セグメント情報の表に XBRL のタグが無い(単位は千円)
    def table(a, b, tot, pa, pb, ptot):
        return f"""<table><tr><td></td><td colspan="3">報告セグメント</td><td>計</td><td>調整額（注）１</td><td>連結財務諸表計上額（注）２</td></tr>
        <tr><td></td><td>放電加工・ 表面処理</td><td>金型</td><td>機械装置等</td><td></td><td></td><td></td></tr>
        <tr><td>外部顧客への売上高</td><td>{a}</td><td>{b}</td><td>669,313</td><td>8,636,492</td><td>―</td><td>{tot}</td></tr>
        <tr><td>計</td><td>6,173,428</td><td>{b}</td><td>707,563</td><td>8,674,929</td><td>△38,436</td><td>{tot}</td></tr>
        <tr><td>セグメント利益</td><td>{pa}</td><td>{pb}</td><td>152,944</td><td>1,833,809</td><td>△701,272</td><td>{ptot}</td></tr></table>"""
    html = "(単位：千円)" + table("4,852,234", "1,646,433", "7,101,295", "1,002,043", "152,208", "616,460") \
        + table("6,173,240", "1,793,937", "8,636,492", "1,469,509", "211,355", "1,132,537")
    x = segments.parse_table_html(html)
    assert [s["name"] for s in x["segments"]] == ["放電加工・表面処理", "金型", "機械装置等"]
    assert (x["segments"][0]["sales"], x["segments"][0]["op"], x["segments"][0]["sales_prior"]) == (6173240e3, 1469509e3, 4852234e3)
    assert (x["total"]["sales"], x["total"]["op"], x["total"]["op_prior"]) == (8636492e3, 1132537e3, 616460e3)
    assert x["tpl"] == {"cols": ["放電加工・表面処理", "金型", "機械装置等", None, None, None], "n": 6, "unit": 1e3}


def test_pdf_external_sales_label_above_unlabeled_numbers():
    # 大和 1Q: 「(1）外部顧客への」「売上高」が上の行にあり、数値の行に見出しが無い。セグメント間の行も同じ形
    text = """計
売上高
(1）外部顧客への
売上高
3,475,289 310,059 154,039 75,651 19,749 4,034,789 89,596 4,124,386 － 4,124,386
(2）セグメント間
の内部売上高又は
振替高
604 8,852 － 67,897 68,671 146,025 - 146,025 △146,025 －
計 3,475,893 318,911 154,039 143,549 88,420 4,180,815 89,596 4,270,412 △146,025 4,124,386
セグメント利益 115,198 11,774 15,152 29,887 6,660 178,674 5,274 183,949 △3,015 180,933
"""
    tpl = {"cols": ["百貨店業", "ホテル業", "出版業", "印刷業", "人材サービス業", None, "その他", None, None, None],
           "n": 10, "unit": 1e3}
    x = segments.parse_pdf_text(text, tpl)
    assert [s["sales"] for s in x["segments"]][3:5] == [75651e3, 19749e3]
    assert x["total"]["sales"] == 4124386e3
