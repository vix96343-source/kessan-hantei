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
