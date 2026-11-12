"""説明資料の四半期推移の表(品目別の売上高・受注高)"""
from src import pres_tables as pt

TEXT = """商品別 売上高・受注高推移
(百万円)
2026年3月期 2027年3月期 前年同期比
２Q 3Q 4Q 1Q ２Q (増減額） (増減率%）
売上高 1,612 1,838 2,124 2,781 3,005 +1,392 +86.4
(商品別内訳)
一般電子部品 990 1,142 1,296 1,746 1,849 +859 +86.8
電 源 274 322 387 476 569 +295 +107.4
電子デバイス 44 48 70 75 86 +42 +96.6
I O T 機 器 65 91 102 88 82 +17 +26.8
セ ン サ ー 17 19 23 32 22 +4 +25.7
そ の 他 220 214 243 362 394 +173 +78.9
受注高 1,716 2,184 2,993 3,515 4,745 +3,028 +176.5
(商品別内訳)
一般電子部品 1,072 1,368 1,854 2,273 3,119 +2,047 +190.8
電 源 237 350 527 594 842 +605 +255.3
電子デバイス 45 52 94 139 128 +83 +183.6
I O T 機 器 97 93 136 68 61 -35 -36.8
セ ン サ ー 15 20 72 20 18 +3 +20.5
そ の 他 248 299 307 418 574 +326 +131.3
"""
HQ = {"2025-09": 1613e6, "2025-12": 1838e6, "2026-03": 2125e6, "2026-06": 2781e6, "2026-09": 3006e6}


def test_reads_items_of_sales_and_orders():
    [t] = pt.parse_text(TEXT, 2, "2026-09", HQ)
    assert t["unit"] == 1e6
    assert [n for n, _ in t["sales"]["items"]] == ["一般電子部品", "電源", "電子デバイス", "IOT機器", "センサー", "その他"]
    assert t["sales"]["items"][0][1]["2026-09"] == 1849e6
    assert t["orders"]["total"]["2026-06"] == 3515e6
    assert dict(t["orders"]["items"])["電源"]["2025-09"] == 237e6
    m = pt.merge_tables([("2026-11-12", t)])
    b = pt.breakdown_data(m, "2026-09", 2, "x")
    assert b["items"][0]["sales"] == (1746 + 1849) * 1e6 and b["items"][0]["sales_prior"] is None
    o = pt.orders_data(m, "2026-09", 2, "x")
    assert o["total"]["orders"] == (3515 + 4745) * 1e6
    assert [s["name"] for s in o["segments"]][:2] == ["一般電子部品", "電源"]


def test_rejects_table_not_matching_consolidated_sales():
    hq = dict(HQ, **{"2026-09": 2900e6})               # 連結の3か月の売上と合わない表は読まない
    assert pt.parse_text(TEXT, 2, "2026-09", hq) == []


def test_rejects_cumulative_table():
    cum = TEXT.replace("売上高 1,612 1,838 2,124 2,781 3,005", "売上高 3,367 5,206 7,330 2,781 5,787")
    assert pt.parse_text(cum, 2, "2026-09", HQ) == []
