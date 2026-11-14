from src import good_rules as gr


def _rules(tmp_path, body):
    p = tmp_path / "good_rules.yaml"
    p.write_text(body, encoding="utf-8")
    return gr.load(p)


def test_any_all_and_missing_values(tmp_path):
    r = _rules(tmp_path, "match: any\nrules:\n  - name: 利益\n    when: op_q_yoy >= 30 and sales_q_yoy >= 10\n"
                         "  - name: 受注\n    when: orders_yoy >= 20\n")
    assert gr.evaluate(r, {"op_q_yoy": 40, "sales_q_yoy": 12}) == ["利益"]
    assert gr.evaluate(r, {"op_q_yoy": 40, "sales_q_yoy": None, "orders_yoy": 25}) == ["受注"]   # 値が無い比較は不成立
    assert gr.evaluate(r, {"op_q_yoy": 10}) is None
    r = _rules(tmp_path, "match: all\nrules:\n  - when: reaction >= 5\n  - when: not revised_up == 0\n")
    assert gr.evaluate(r, {"reaction": 6, "revised_up": 1}) == ["基準1", "基準2"]
    assert gr.evaluate(r, {"reaction": 6, "revised_up": 0}) is None


def test_bad_rules_are_reported_not_run(tmp_path):
    r = _rules(tmp_path, "rules:\n  - name: 関数\n    when: __import__('os')\n  - name: 名前\n    when: foo > 1\n"
                         "  - name: 正しい\n    when: (op_qoq - 10) / 2 > 5\n")
    assert [x["name"] for x in r["rules"]] == ["正しい"]
    assert len(r["errors"]) == 2
    assert gr.evaluate(r, {"op_qoq": 30}) == ["正しい"]


def test_no_rules_means_nothing(tmp_path):
    r = _rules(tmp_path, "match: any\nrules:\n")
    assert gr.evaluate(r, {"op_q_yoy": 100}) is None
