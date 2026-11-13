from src import breakdown

HTML = """<p>３．補足情報</p><p>（品目別実績）</p><table>
<tr><td></td><td></td><td>前中間連結会計期間</td><td>前中間連結会計期間</td><td>当中間連結会計期間</td><td>当中間連結会計期間</td><td>増減率（％）</td></tr>
<tr><td></td><td></td><td>金額（百万円）</td><td>構成比（％）</td><td>金額（百万円）</td><td>構成比（％）</td><td>増減率（％）</td></tr>
<tr><td></td><td>メモリ</td><td>194,822</td><td>80.2</td><td>753,945</td><td>95.9</td><td>287.0</td></tr>
<tr><td></td><td>システムLSI</td><td>40,701</td><td>16.7</td><td>25,454</td><td>3.2</td><td>△37.5</td></tr>
<tr><td>その他</td><td>その他</td><td>7,566</td><td>3.1</td><td>6,441</td><td>0.9</td><td>△14.9</td></tr>
<tr><td>合計</td><td>合計</td><td>243,089</td><td>100.0</td><td>785,840</td><td>100.0</td><td>223.3</td></tr></table>"""

PDF = "\n".join([
    "メモリ 100,000 80.0 120,000 85.0 20.0",
    "システムLSI 20,000 16.0 15,000 10.6 △25.0",
    "その他 5,000 4.0 6,000 4.3 20.0",
    "合計 125,000 100.0 141,000 100.0 12.8",
])


def test_breakdown_table_checked_against_consolidated_sales():
    x = breakdown.parse_html(HTML, 785840e6)
    assert x["title"] == "品目別実績"
    assert [(i["name"], i["sales"], i["sales_prior"]) for i in x["items"]][:2] == \
        [("メモリ", 753945e6, 194822e6), ("システムLSI", 25454e6, 40701e6)]
    assert breakdown.parse_html(HTML, 700000e6) is None                     # 合計が連結売上と合わない表は読まない
    y = breakdown.parse_pdf_text(PDF, x["tpl"], 141000e6)                   # 過去の PDF を同じ形で
    assert [i["sales"] for i in y["items"]] == [120000e6, 15000e6, 6000e6]


def test_product_line_by_segment_matrix_with_period_above_table():
    """ツガミ: 製品ライン×セグメントの表(列は 日本 中国 … 合計)が前期・当期の2つ。期間は表の上の文"""
    def tbl(rows):
        head = "<tr><td></td><td>報告セグメント</td><td>報告セグメント</td><td>報告セグメント</td></tr>" \
               "<tr><td></td><td>日本</td><td>中国</td><td>合計</td></tr><tr><td>主要な製品ライン</td><td></td><td></td><td></td></tr>"
        return "<table>" + head + "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows) + "</table>"
    html = ("<p>（2）連結販売実績</p><p>前中間連結会計期間（自 2025年４月１日 至 2025年９月30日）</p><p>（単位：百万円）</p>"
            + tbl([["自動旋盤", "5,923", "43,448", "49,371"], ["研削盤", "336", "2,644", "2,980"], ["その他", "1,401", "1,701", "3,102"],
                   ["合計", "7,660", "47,793", "55,453"]])
            + "<p>当中間連結会計期間（自 2026年４月１日 至 2026年９月30日）</p><p>（単位：百万円）</p>"
            + tbl([["自動旋盤", "7,678", "60,675", "68,353"], ["研削盤", "383", "2,686", "3,069"], ["その他", "1,473", "2,005", "3,478"],
                   ["合計", "9,534", "65,366", "74,900"]]))
    x = breakdown.parse_html(html, 74_900e6, ["日本", "中国"])
    assert [(i["name"], i["sales"], i["sales_prior"]) for i in x["items"]] == [
        ("自動旋盤", 68_353e6, 49_371e6), ("研削盤", 3_069e6, 2_980e6), ("その他", 3_478e6, 3_102e6)]
    assert x["total"] == {"sales": 74_900e6, "sales_prior": 55_453e6} and x["title"] == "製品ライン別"
