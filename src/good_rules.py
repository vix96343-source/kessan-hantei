"""好決算の基準(ユーザーが good_rules.yaml に書く)を読み、決算ごとの数値に当てはめる。

基準は1行の式(例: "op_q_yoy >= 30 and orders_yoy >= 20")。使える名前は METRICS。
値が無い名前(受注を出していない会社の orders_yoy など)を使った比較は「当てはまらない」になる。
式は四則演算・比較・and / or / not だけを許す(関数呼び出しなどは読み込み時にはじく)。
"""
from __future__ import annotations

import ast
import logging
from pathlib import Path

import yaml

from .config import ROOT

log = logging.getLogger(__name__)
RULES_FILE = ROOT / "good_rules.yaml"

# 名前 → 説明(good_rules.yaml の説明と同じ)
METRICS = {
    "sales_yoy": "売上高 累計の前年同期比(%)", "op_yoy": "営業利益 累計の前年同期比(%)",
    "ordinary_yoy": "経常利益 累計の前年同期比(%)", "net_yoy": "純利益 累計の前年同期比(%)",
    "sales_q_yoy": "売上高 3か月の前年同期比(%)", "op_q_yoy": "営業利益 3か月の前年同期比(%)",
    "ordinary_q_yoy": "経常利益 3か月の前年同期比(%)", "net_q_yoy": "純利益 3か月の前年同期比(%)",
    "sales_qoq": "売上高 3か月の前四半期比(%)", "op_qoq": "営業利益 3か月の前四半期比(%)",
    "ordinary_qoq": "経常利益 3か月の前四半期比(%)", "net_qoq": "純利益 3か月の前四半期比(%)",
    "sales": "売上高 3か月(百万円)", "op": "営業利益 3か月(百万円)",
    "ordinary": "経常利益 3か月(百万円)", "net": "純利益 3か月(百万円)",
    "margin": "営業利益率 3か月(%)", "margin_yoy_pt": "営業利益率 3か月の前年同期からの差(pt)",
    "orders_yoy": "受注高の前年同期比(%)", "orders_qoq": "受注高 3か月の前四半期比(%)",
    "backlog_yoy": "受注残高の前年同期比(%)", "backlog_qoq": "受注残高の前四半期末比(%)",
    "progress": "営業利益の会社予想(通期)に対する進捗率(%)",
    "revised": "短信で業績予想を修正した(1/0)",
    "reaction": "株価の反応(%): 発表前の終値 → 発表後の終値",
    # 1. 期待に対する上振れ
    "cons_beat": "経常利益の累計 ÷ コンセンサス(株予報、発表前)の同じ四半期累計 − 1(%)。本決算は通期",
    "guidance_vs_cons": "会社予想(通期の営業利益) ÷ コンセンサスの通期営業利益 − 1(%)。本決算は来期予想どうし",
    "progress_gap": "営業利益の進捗率 − 過去3年の同じ四半期の進捗率の平均(pt)。会社計画に対する上振れ",
    "revised_up": "決算と同じ日に業績予想を上方修正した(1/0)",
    # 2. 中身の上振れ
    "seg_op_yoy_max": "セグメント利益の前年同期比(累計)の一番大きいセグメントの値(%)",
    "seg_op_yoy_min": "セグメント利益の前年同期比(累計)の一番小さいセグメントの値(%)",
    "orders_record": "3か月の受注高が過去(4四半期以上)の最高を更新(1/0)",
    "backlog_record": "受注残高が過去(4四半期以上)の最高を更新(1/0)",
    # 3. 伸び方
    "sales_qoq_vs_ly": "売上高の前四半期比 − 前年の同じ四半期の前四半期比(pt)。季節性をならした伸び",
    "op_qoq_vs_ly": "営業利益の前四半期比 − 前年の同じ四半期の前四半期比(pt)",
    "margin_qoq_pt": "営業利益率 3か月の前四半期からの差(pt)",
    "seg_margin_up": "利益率が前年同期より上がったセグメントの割合(%)",
}

_OK = (ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not, ast.USub, ast.UAdd, ast.Compare,
       ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Eq, ast.NotEq, ast.BinOp, ast.Add, ast.Sub, ast.Mult, ast.Div,
       ast.Name, ast.Load, ast.Constant)


def _compile(expr: str) -> ast.Expression:
    tree = ast.parse(str(expr), mode="eval")
    for n in ast.walk(tree):
        if not isinstance(n, _OK):
            raise ValueError(f"使えない書き方: {type(n).__name__}")
        if isinstance(n, ast.Name) and n.id not in METRICS:
            raise ValueError(f"知らない名前: {n.id}")
        if isinstance(n, ast.Constant) and not isinstance(n.value, (int, float)):
            raise ValueError(f"数値以外の値: {n.value!r}")
    return tree


def _ev(n, m: dict):
    """None(値が無い)は比較で False、計算で None"""
    if isinstance(n, ast.Expression):
        return _ev(n.body, m)
    if isinstance(n, ast.Constant):
        return n.value
    if isinstance(n, ast.Name):
        return m.get(n.id)
    if isinstance(n, ast.BoolOp):
        vals = (bool(_ev(v, m)) for v in n.values)
        return all(vals) if isinstance(n.op, ast.And) else any(vals)
    if isinstance(n, ast.UnaryOp):
        v = _ev(n.operand, m)
        if isinstance(n.op, ast.Not):
            return not v
        return None if v is None else (-v if isinstance(n.op, ast.USub) else v)
    if isinstance(n, ast.BinOp):
        a, b = _ev(n.left, m), _ev(n.right, m)
        if a is None or b is None:
            return None
        if isinstance(n.op, ast.Div):
            return None if b == 0 else a / b
        return a + b if isinstance(n.op, ast.Add) else a - b if isinstance(n.op, ast.Sub) else a * b
    if isinstance(n, ast.Compare):
        left = _ev(n.left, m)
        for op, c in zip(n.ops, n.comparators):
            right = _ev(c, m)
            if left is None or right is None:
                return False
            ok = {ast.Lt: left < right, ast.LtE: left <= right, ast.Gt: left > right, ast.GtE: left >= right,
                  ast.Eq: left == right, ast.NotEq: left != right}[type(op)]
            if not ok:
                return False
            left = right
        return True
    raise ValueError(type(n).__name__)


def load(path: Path = RULES_FILE) -> dict:
    """{"match": "any"|"all", "rules": [{"name", "when", "tree"}], "errors": [...]}。書き間違いの基準は飛ばして errors に"""
    out = {"match": "any", "rules": [], "errors": []}
    if not Path(path).exists():
        return out
    try:
        y = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        out["errors"].append(f"good_rules.yaml を読めない: {e}")
        return out
    out["match"] = "all" if str(y.get("match", "any")).strip() == "all" else "any"
    for i, r in enumerate(y.get("rules") or [], 1):
        if not isinstance(r, dict) or not r.get("when"):
            continue
        name = str(r.get("name") or f"基準{i}")
        try:
            out["rules"].append({"name": name, "when": str(r["when"]), "tree": _compile(r["when"])})
        except (SyntaxError, ValueError) as e:
            out["errors"].append(f"{name}: {e}")
            log.warning("好決算の基準 %s を飛ばす: %s", name, e)
    return out


def file_hash(path: Path = RULES_FILE) -> str:
    """基準のファイルが変わったか(監視がサイトを作り直すかの判断用)"""
    import hashlib
    return hashlib.sha1(Path(path).read_bytes()).hexdigest()[:12] if Path(path).exists() else ""


def evaluate(rules: dict, m: dict) -> list[str] | None:
    """当てはまった基準の名前。好決算でなければ None(基準が1つも無いときも None)"""
    if not rules["rules"]:
        return None
    hits = [r["name"] for r in rules["rules"] if _ev(r["tree"], m)]
    good = len(hits) == len(rules["rules"]) if rules["match"] == "all" else bool(hits)
    return hits if good else None
