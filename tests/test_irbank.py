from src.datasources import irbank


def _page(caption: str) -> str:
    # IRBANK の四半期ページの表(必要な所だけ)。各四半期の単独値は .shihanki
    return f"""
<table id="graph"><caption>{caption}</caption>
<thead><tr><th>年度</th><th>四半期</th><th>売上収益</th><th>営業利益</th><th>当期利益</th></tr></thead>
<tbody>
<tr><td class="lf">2026年 8月期 連結</td></tr>
<tr><td class="lf"><a title="2026年1月8日">1Q 実績</a></td>
<td><span class="shihanki">+10,277</span> 10,277</td><td><span class="shihanki">+2,011</span> 2,011</td>
<td><span class="shihanki">+1,437</span> 1,437</td></tr>
</tbody></table>"""


def test_large_companies_are_shown_in_oku_yen():
    # ファーストリテイリングなどは「四半期毎履歴（億円）」。百万円として読むと 1/100 になる
    rows = irbank.parse_quarterly(_page("四半期毎履歴（億円）"))
    assert rows[0]["end"] == "2025-11" and rows[0]["sales"] == 10277e8 and rows[0]["op"] == 2011e8


def test_default_unit_is_million_yen():
    rows = irbank.parse_quarterly(_page("四半期毎履歴（百万円）"))
    assert rows[0]["sales"] == 10277e6
