from src import orders
from src.site import order_numbers

# ローツェ 2027年2月期2Q の短信(品目の入れ子・前年同期比=前年を100とした比率)
RORZE = """
<p>(2) 受注実績</p>
<table>
<tr><td colspan="2" rowspan="2">セグメントの名称</td><td rowspan="2">受注高 (百万円)</td><td rowspan="2">前年同期比(％)</td>
<td rowspan="2">受注残高 (百万円)</td><td rowspan="2">前年同期比(％)</td></tr>
<tr></tr>
<tr><td></td><td>品目</td><td></td><td></td><td></td><td></td></tr>
<tr><td colspan="6">半導体・ＦＰＤ関連装置事業</td></tr>
<tr><td></td><td>半導体関連装置</td><td>120,540</td><td>256.4</td><td>103,863</td><td>229.8</td></tr>
<tr><td></td><td>分析装置</td><td>3,712</td><td>294.4</td><td>5,604</td><td>166.5</td></tr>
<tr><td></td><td>ＦＰＤ関連装置</td><td>3,386</td><td>206.5</td><td>1,127</td><td>115.6</td></tr>
<tr><td colspan="2">計</td><td>127,639</td><td>255.7</td><td>110,595</td><td>223.3</td></tr>
<tr><td colspan="2">ライフサイエンス事業</td><td>907</td><td>227.9</td><td>889</td><td>375.2</td></tr>
<tr><td colspan="2">合計</td><td>128,546</td><td>255.5</td><td>111,485</td><td>224.0</td></tr>
</table>
"""

# 増減率で書く表(△あり)
GROWTH = """
<table>
<tr><td rowspan="2">種類</td><td colspan="4">当第３四半期連結累計期間</td></tr>
<tr><td>受注高（百万円）</td><td>前年同期増減率（％）</td><td>受注残高（百万円）</td><td>前年同期増減率（％）</td></tr>
<tr><td>ソフトウェア</td><td>11,628</td><td>12.7</td><td>9,300</td><td>46.0</td></tr>
<tr><td>その他</td><td>703</td><td>△26.4</td><td>311</td><td>△14.3</td></tr>
<tr><td>合計</td><td>12,331</td><td>10.0</td><td>9,611</td><td>42.0</td></tr>
</table>
"""

# 建設業: 行が項目、列が期間(前年・当年・増減額・増減率)
CONSTRUCTION = """
<table>
<tr><td rowspan="2">項　目</td><td>前中間連結会計期間</td><td>当中間連結会計期間</td><td colspan="2">対前年同期</td></tr>
<tr><td>金額 (千円)</td><td>金額 (千円)</td><td>金額 (千円)</td><td>増減率 (％)</td></tr>
<tr><td>前期繰越工事高</td><td>7,197,382</td><td>8,512,120</td><td>1,314,738</td><td>18.3</td></tr>
<tr><td>当期受注工事高</td><td>4,186,451</td><td>6,198,960</td><td>2,012,508</td><td>48.1</td></tr>
<tr><td>当期完成工事高</td><td>4,936,275</td><td>5,859,616</td><td>923,341</td><td>18.7</td></tr>
<tr><td>次期繰越工事高</td><td>6,447,558</td><td>8,851,464</td><td>2,403,906</td><td>37.3</td></tr>
</table>
"""

# 前年・当年・前期末が並ぶ表(単位は別の行)
PERIODS = """
<table>
<tr><td>期別</td><td colspan="2">前第1四半期累計期間</td><td colspan="2">当第1四半期累計期間</td><td colspan="2">前事業年度</td></tr>
<tr><td>区分</td><td>受注高</td><td>受注残高</td><td>受注高</td><td>受注残高</td><td>受注高</td><td>受注残高</td></tr>
<tr><td></td><td>千円</td><td>千円</td><td>千円</td><td>千円</td><td>千円</td><td>千円</td></tr>
<tr><td>学校アルバム</td><td>172,827</td><td>69,679</td><td>183,454</td><td>79,706</td><td>1,708,250</td><td>159,313</td></tr>
<tr><td>一般商業印刷</td><td>112,444</td><td>12,460</td><td>119,995</td><td>10,640</td><td>405,527</td><td>8,120</td></tr>
<tr><td>合　計</td><td>285,272</td><td>82,139</td><td>303,449</td><td>90,346</td><td>2,113,808</td><td>167,433</td></tr>
</table>
"""


def test_rorze_ratio_style_and_nesting():
    x = orders.parse_qualitative(RORZE)
    t = x["total"]
    assert t["orders"] == 128546e6 and t["backlog"] == 111485e6
    assert t["orders_yoy"] == 155.5 and t["backlog_yoy"] == 124.0       # 255.5 → +155.5%
    assert [s["name"] for s in x["segments"]] == ["半導体関連装置", "分析装置", "ＦＰＤ関連装置", "ライフサイエンス事業"]
    assert x["segments"][0]["orders_yoy"] == 156.4


def test_growth_style():
    x = orders.parse_qualitative(GROWTH)
    assert x["total"]["orders_yoy"] == 10.0
    assert x["segments"][1]["orders_yoy"] == -26.4 and x["segments"][1]["backlog_yoy"] == -14.3


def test_construction_rows():
    x = orders.parse_qualitative(CONSTRUCTION)
    t = x["total"]
    assert t["orders"] == 6198960e3 and t["backlog"] == 8851464e3
    assert t["orders_yoy"] == 48.1 and t["backlog_yoy"] == 37.3
    assert x["segments"] == []


def test_period_columns_and_previous_year_end():
    x = orders.parse_qualitative(PERIODS)
    t = x["total"]
    assert t["orders"] == 303449e3 and t["orders_prior"] == 285272e3 and t["backlog_fy"] == 167433e3
    assert t["orders_yoy"] == round((303449 / 285272 - 1) * 100, 1)


def test_no_orders_table():
    assert orders.parse_qualitative("<table><tr><td>受注損失引当金</td><td>1,000</td></tr></table>") is None


def test_qoq_from_previous_report():
    def rec(n_q, end, orders_, backlog, at):
        return {"disclosure_id": at, "disclosed_at": at, "n_q": str(n_q), "period_end": end,
                "data": {"total": {"name": "合計", "orders": orders_, "backlog": backlog, "orders_yoy": None,
                                   "backlog_yoy": None, "backlog_fy": None}, "segments": []}}
    q1 = rec(1, "2026-05-31", 100e6, 300e6, "2026-07-10")
    q2 = rec(2, "2026-08-31", 250e6, 330e6, "2026-10-08")      # 2Q単独 = 250 − 100 = 150
    o = order_numbers(q2, [q1])["total"]
    assert o["orders"]["qoq"] == 50.0 and o["backlog"]["qoq"] == 10.0
    q3 = rec(3, "2026-11-30", 400e6, 360e6, "2027-01-10")      # 3Q単独 = 150 → 前四半期比 0%
    assert order_numbers(q3, [q1, q2])["total"]["orders"]["qoq"] == 0.0


def test_previous_quarter_pdf_text_with_current_table_shape():
    # 今回の短信(HTML)の表の形を使い、前の期の短信 PDF の文字(行ごと)を読む。本文や生産実績の同じ行見出しは拾わない
    tpl = orders.parse_qualitative(RORZE)["tpl"]
    pdf = "\n".join([
        "（３）補足情報 生産、受注及び販売の状況 ……… 10",
        "当第１四半期の受注高は46,529百万円となりました。",
        "(1) 生産実績",
        "半導体関連装置 19,000 120.0",
        "(2) 受注実績",
        "セグメントの名称 受注高",
        "(百万円)",
        "前年同期比",
        "(％)",
        "受注残高",
        "(百万円)",
        "前年同期比",
        "(％)品目",
        "半導体・ＦＰＤ関連装置事業",
        "半導体関連装置 42,030 188.4 62,253 132.7",
        "分析装置 2,366 396.3 4,672 134.1",
        "ＦＰＤ関連装置 1,390 119.2 1,037 37.6",
        "計 45,788 190.2 67,963 127.9",
        "ライフサイエンス事業 741 502.8 772 539.1",
        "合計 46,529 192.1 68,736 129.0",
    ])
    x = orders.parse_pdf_text(pdf, tpl, 1e6)
    assert x["total"]["orders"] == 46529e6 and x["total"]["backlog"] == 68736e6
    assert x["total"]["orders_yoy"] == 92.1 and x["segments"][0]["orders"] == 42030e6
