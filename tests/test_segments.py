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
    assert g["total"]["sales"]["v"] == 10975000                           # 短信の表の合計が無ければ一覧の数値(百万円)を千円で
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
