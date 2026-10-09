import io
import zipfile
from datetime import date

import pytest

from src.datasources import tdnet

LIST_HTML = """
<div class="kaijiSum">1～3件&nbsp;/&nbsp;全3件</div>
<table id="main-list-table">
<tr>
<td class="oddnew-L kjTime" noWrap>16:30</td>
<td class="oddnew-M kjCode" noWrap>26870</td>
<td class="oddnew-M kjName" noWrap>テスト商事   </td>
<td class="oddnew-M kjTitle"><a href="140120261008547896.pdf">業績予想の修正に関するお知らせ</a></td>
<td class="oddnew-M kjXbrl"><div class="xbrl-button"><a href="091220261008547896.zip">XBRL</a></div></td>
<td class="oddnew-M kjPlace">東  </td>
</tr>
<tr>
<td class="evennew-L kjTime" noWrap>15:00</td>
<td class="evennew-M kjCode" noWrap>130A0</td>
<td class="evennew-M kjName" noWrap>英字コード</td>
<td class="evennew-M kjTitle"><a href="140120261008500001.pdf">2027年3月期 第2四半期（中間期）決算短信〔日本基準〕（連結）</a></td>
<td class="evennew-M kjXbrl"> </td>
<td class="evennew-M kjPlace">東</td>
</tr>
<tr>
<td class="oddnew-L kjTime" noWrap>15:00</td>
<td class="oddnew-M kjCode" noWrap>72030</td>
<td class="oddnew-M kjName" noWrap>テスト自動車</td>
<td class="oddnew-M kjTitle"><a href="140120261008500002.pdf">自己株式の取得状況に関するお知らせ</a></td>
<td class="oddnew-M kjXbrl"> </td>
<td class="oddnew-M kjPlace">東</td>
</tr>
</table>
"""


def test_parse_list_page():
    items, total = tdnet.parse_list_page(LIST_HTML, date(2026, 10, 8))
    assert total == 3
    assert len(items) == 3
    a = items[0]
    assert a.disclosure_id == "140120261008547896"
    assert a.disclosed_at == "2026-10-08T16:30"
    assert a.code == "2687"
    assert a.name == "テスト商事"
    assert a.xbrl_url.endswith("091220261008547896.zip")
    assert items[1].code == "130A"
    assert items[1].xbrl_url == ""


@pytest.mark.parametrize("title,kind,corr,direction", [
    ("業績予想の修正に関するお知らせ", "forecast_revision", False, ""),
    ("通期業績予想の上方修正に関するお知らせ", "forecast_revision", False, "up"),
    ("2027年3月期通期連結業績予想の修正(下方修正)に関するお知らせ", "forecast_revision", False, "down"),
    ("通期連結業績予想の修正及び配当予想の修正に関するお知らせ", "forecast_dividend_revision", False, ""),
    ("業績予想及び配当予想の修正（増配）に関するお知らせ", "forecast_dividend_revision", False, "up"),
    ("配当予想の修正（増配）に関するお知らせ", "dividend_revision", False, "up"),
    ("配当予想の修正(創業100周年記念配当の実施)に関するお知らせ", "dividend_revision", False, ""),
    ("第2四半期連結業績予想と実績値との差異に関するお知らせ", "actual_vs_forecast", False, ""),
    ("中間期の連結業績予想と実績との差異および通期連結業績予想の修正に関するお知らせ", "forecast_revision", False, ""),
    ("2026年11月期通期連結業績予想に関するお知らせ", "forecast_initial", False, ""),
    ("2027年2月期 第2四半期（中間期）決算短信〔日本基準〕（連結）", "earnings_report", False, ""),
    ("2027年2月期 第2四半期（中間期）決算説明資料", "earnings_presentation", False, ""),
    ("2027年2月期第２四半期決算短信補足", "earnings_presentation", False, ""),
    ("(訂正)適時開示書類「通期業績予想の修正(下方修正)に関するお知らせ」の一部訂正",
     "forecast_revision", True, "down"),
    ("自己株式の取得状況に関するお知らせ", "other", False, ""),
    ("NEXT FUNDS 東証REIT指数連動型上場投信 決算短信", "other", False, ""),
    ("2027年2月期第2四半期 連結決算の概要", "earnings_presentation", False, ""),
    ("2027年２月期　第２四半期　決算・参考資料", "earnings_presentation", False, ""),
    ("2027年2月期　第2四半期（中間期）決算ハイライト", "earnings_presentation", False, ""),
    ("2027年3月期 第2四半期 決算説明会資料", "earnings_presentation", False, ""),
    ("FY2026 Q2 Financial Results Presentation", "earnings_presentation", False, ""),
    ("2026年８月期 決算短信〔ＩＦＲＳ会計基準〕（連結）", "earnings_report", False, ""),
    ("決算発表日の変更に関するお知らせ", "other", False, ""),
    ("中期経営計画策定に関するお知らせ", "other", False, ""),
    ("2027年3月期第2四半期決算説明会開催のお知らせ", "other", False, ""),
    ("ユーロ円CB発行および自己株式取得に関する補足説明資料", "other", False, ""),
    ("株主優待制度（分配型）に関する補足説明のお知らせ", "other", False, ""),
    ("2026年８月期　決算説明資料に関するお知らせ", "earnings_presentation", False, ""),
    ("「2027年5月期 第1四半期決算概要」のお知らせ", "earnings_presentation", False, ""),
    ("2027年１月期 第２四半期決算説明会 書き起こし公開のお知らせ", "other", False, ""),
])
def test_classify_title(title, kind, corr, direction):
    c = tdnet.classify_title(title)
    assert (c["kind"], c["is_correction"], c["title_direction"]) == (kind, corr, direction)


def _ix(name, ctx, value=None, sign=False):
    s = ' sign="-"' if sign else ""
    if value is None:
        return f'<ix:nonFraction name="tse-ed-t:{name}" contextRef="{ctx}" xsi:nil="true"/>'
    return f'<ix:nonFraction name="tse-ed-t:{name}" contextRef="{ctx}" scale="6"{s}>{value}</ix:nonFraction>'


def _zip(body: str, fname="tse-rvfc-26870-20261008547896-ixbrl.htm") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(fname, f"<html><body>{body}</body></html>")
    return buf.getvalue()


FY = "CurrentYearDuration_ConsolidatedMember"
Q2 = "CurrentAccumulatedQ2Duration_ConsolidatedMember"


def test_parse_xbrl_uses_change_rate_and_skips_nil():
    body = "".join([
        _ix("NetSales", f"{Q2}_CurrentMember_ForecastMember", "4,024"),
        _ix("NetSales", f"{FY}_PreviousMember_ForecastMember", "8,094"),
        _ix("NetSales", f"{FY}_PreviousMember_UpperMember"),          # 空要素で値がずれないこと
        _ix("OperatingIncome", f"{FY}_PreviousMember_ForecastMember", "414"),
        _ix("OperatingIncome", f"{FY}_CurrentMember_ForecastMember", "309"),
        _ix("ChangeInNetSales", f"{FY}_CurrentMember_ForecastMember", "0.8"),
        _ix("ChangeInOperatingIncome", f"{FY}_CurrentMember_ForecastMember", "25.4", sign=True),
    ])
    x = tdnet.parse_forecast_revision_xbrl(_zip(body))
    assert x["period"] == "CurrentYearDuration"
    assert x["metric"] == "OperatingIncome"
    assert x["change_pct"] == -25.4
    assert x["direction"] == "down"
    assert x["changes"]["NetSales"] == 0.8


def test_parse_xbrl_computes_rate_from_range_when_change_missing():
    body = "".join([
        _ix("OrdinaryIncome", f"{FY}_PreviousMember_ForecastMember", "100"),
        _ix("OrdinaryIncome", f"{FY}_CurrentMember_UpperMember", "130"),
        _ix("OrdinaryIncome", f"{FY}_CurrentMember_LowerMember", "110"),
    ])
    x = tdnet.parse_forecast_revision_xbrl(_zip(body))
    assert x["metric"] == "OrdinaryIncome"
    assert x["change_pct"] == 20.0
    assert x["direction"] == "up"


def test_parse_xbrl_bad_input():
    assert tdnet.parse_forecast_revision_xbrl(b"not a zip") == {}
    assert tdnet.parse_forecast_revision_xbrl(_zip("", fname="other.htm")) == {}
