"""説明資料の「前年 / 今年 / 増減」のセグメント別受注高"""
from src import pres_seg_orders as ps


def test_table_with_orders_and_backlog_in_one_row():
    # JFEシステムズ: 受注高と受注残高が1行に並ぶ表。増減率だけの列(△5.0% は整数に見えても増減率)
    text = """2026年3月期 事業別受注高および受注残高の状況(連結)
合計 67,059 54,589 △18.6% 25,885 23,064 △10.9%
デジタル製造 2,416 1,466 △39.3% 2,650 1,890 △28.7%
ERPソリューション 3,673 3,728 +1.5% 1,830 1,597 △12.7%
鉄鋼 54,849 43,582 △20.5% 19,312 17,594 △8.9%
子会社、他 6,121 5,813 △5.0% 2,093 1,983 △5.2%
単位:百万円"""
    rec = {"total": {"orders": 54_589e6, "backlog": 23_064e6, "orders_prior": None}}
    x = ps.read(text, rec, [])
    assert x["鉄鋼"]["orders"] == 43_582e6 and x["鉄鋼"]["orders_prior"] == 54_849e6
    assert x["子会社、他"]["backlog"] == 1_983e6


def test_pages_named_by_heading_or_remaining_segment():
    # 栗田工業: 事業ごとのページに「受注高 前年 今年 増減」。1つは見出しで、もう1つは残りのセグメント
    p0 = "業績概要\n(単位:億円)\n受注高 1,978 3,596 + 1,618 + 81.8% 2,600\n売上高 1,893 2,069 + 175 + 9.3% 1,980"
    p1 = "電子 事業別業績\n上期予想\n受注高 786 2,313 + 1,527 1,450\n売上高 811 937 + 126 930"
    p2 = "事業別業績\n上期予想\n受注高 1,192 1,283 + 90 1,150\n売上高 1,082 1,131 + 49 1,050"
    rec = {"total": {"orders": 359_587e6, "orders_prior": 197_800e6}}
    x = ps.read("\f".join([p0, p1, p2]), rec, ["電子市場", "一般水処理市場"])
    assert x["電子市場"]["orders"] == 2_313e8 and x["一般水処理市場"]["orders"] == 1_283e8


def test_rejects_when_segments_do_not_add_up():
    text = "合計 100 120 +20 +20.0%\nA事業 50 60 +10 +20.0%\nB事業 40 50 +10 +25.0%"
    assert ps.read(text, {"total": {"orders": 120e6}}, []) is None


def test_apply_manual_patches_total_and_segments(tmp_path):
    import json
    import pandas as pd
    from src import orders, pres_seg_orders as pso
    d = {"total": {"name": "合計", "orders": 17509e6, "orders_yoy": 8.4, "backlog": None}, "segments": []}
    row = {c: "" for c in orders.COLUMNS}
    row.update({"disclosure_id": "X1", "code": "8157", "status": "ok", "disclosed_at": "2026-10-30T12:30",
                "period_end": "2026-09-30", "n_q": 2, "data": json.dumps(d, ensure_ascii=False)})
    pd.DataFrame([row]).reindex(columns=orders.COLUMNS).to_csv(orders.path(tmp_path), index=False, encoding="utf-8")
    man = {"X1": {"total": {"orders": 61546e6, "orders_prior": 55973e6},
                  "segments": [{"name": "A", "orders": 17509e6, "orders_prior": 16147e6, "backlog": None, "backlog_prior": None},
                               {"name": "B", "orders": 44036e6, "orders_prior": 39825e6, "backlog": None, "backlog_prior": None}],
                  "seg_src": "tanshin_fix", "seg_doc": "短信"}}
    (tmp_path / pso.MANUAL).write_text(json.dumps(man, ensure_ascii=False), encoding="utf-8")
    assert pso.apply_manual(tmp_path) == 1
    out = json.loads(orders.load(tmp_path).iloc[0]["data"])
    assert out["total"]["orders"] == 61546e6 and out["total"]["orders_yoy"] == 10.0
    assert [s["orders_yoy"] for s in out["segments"]] == [8.4, 10.6]
    assert pso.apply_manual(tmp_path) == 0          # 2回目は変わらない
