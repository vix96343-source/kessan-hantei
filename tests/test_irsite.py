from datetime import date

from src import site
from src.datasources import irsite, tdnet

ANN = date(2026, 10, 9)


def test_score_picks_this_periods_presentation():
    good = irsite.score("https://www.yaskawa.co.jp/wp-content/uploads/2026/10/20261009_haifu.pdf",
                        "2027年2月期 第2四半期業績概要", ANN, "2Q", "2027年2月期")
    assert good >= 9


def test_score_rejects_tanshin_old_and_english():
    # 短信そのもの(ファイル名に「決算短信」)
    assert irsite.score("https://x.co.jp/uploads/2026/10/2026年11月期第３四半期決算短信〔日本基準〕連結.pdf",
                        "→こちらからご覧いただけます。", ANN, "3Q", "2026年11月期") < 9
    # 去年の同じ四半期の資料
    assert irsite.score("https://x.co.jp/wp-content/uploads/2025/10/setsumei.pdf",
                        "2026年２月期第２四半期（中間期）決算説明会資料", ANN, "2Q", "2027年2月期") < 9
    # 英語版は日本語版より低い
    ja = irsite.score("https://x.co.jp/ir/2026/10/kessan_2q.pdf", "2027年2月期 第2四半期 決算説明資料", ANN, "2Q", "2027年2月期")
    en = irsite.score("https://x.co.jp/ir/2026/10/kessan_2q_e.pdf", "2027年2月期 第2四半期 決算説明資料 English", ANN, "2Q", "2027年2月期")
    assert ja > en


def test_fy_label_of():
    assert irsite.fy_label_of("2027年２月期 第２四半期（中間期）決算短信") == "2027年2月期"


def test_yes_no_reads_quarterly_and_annual_names():
    assert tdnet._yes_no({"SupplementalMaterialOfResults": "有"}, "SupplementalMaterialOfResults",
                         "SupplementalMaterialOfAnnualResults") is True
    assert tdnet._yes_no({"SupplementalMaterialOfAnnualResults": "無"}, "SupplementalMaterialOfResults",
                         "SupplementalMaterialOfAnnualResults") is False
    assert tdnet._yes_no({}, "SupplementalMaterialOfResults") is None


def test_pdf_links_uses_ir_site_only_when_tdnet_has_none():
    earn = {"kind": "earnings_report", "code": "6506", "disclosure_id": "d1",
            "disclosed_at": "2026-10-09T16:00", "pdf_url": "tanshin.pdf"}
    ir = {"d1": {"url": "https://www.yaskawa.co.jp/haifu.pdf", "title": "業績概要"}}
    links = site.pdf_links(earn, {}, ir)
    assert [(l["label"], l["url"]) for l in links] == [("短信", "tanshin.pdf"), ("資料", "https://www.yaskawa.co.jp/haifu.pdf")]
    tdnet_pres = {"6506": [{"disclosed_at": "2026-10-09T16:00", "pdf_url": "tdnet.pdf", "title": "決算説明資料"}]}
    assert [l["url"] for l in site.pdf_links(earn, tdnet_pres, ir)] == ["tanshin.pdf", "tdnet.pdf"]


def test_score_rejects_qa_script_and_other_notices():
    for text in ["2026年8月期 第3四半期決算説明会の主な質疑応答", "【スクリプト】2027年２月期第２四半期決算説明会",
                 "2027年1月期第2四半期（中間期）の業績予想と実績値との差異に関するお知らせ"]:
        assert irsite.score("https://x.co.jp/ir/2026/10/doc.pdf", text, ANN, "2Q", "2027年2月期") < 9
    # 「決算資料」の見出しの下にある短信(TDnet の書類番号のファイル)
    assert irsite.score("https://x.co.jp/files/140120261008547872.pdf", "決算資料 2026年８月期 決算短信〔日本基準〕",
                        ANN, "通期", "2026年8月期") < 9


def test_score_penalizes_other_quarter():
    assert irsite.score("https://x.co.jp/doc/2026/03/a.pdf", "2026年8月期 第2四半期決算説明資料",
                        ANN, "通期", "2026年8月期") < 9
