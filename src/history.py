"""単独四半期の業績履歴 data/quarterly_history.csv(型判定の土台)。

- 初回: 手元のPCで IRBANK から過去 N 年分を取得(bootstrap)。IRBANK・株探は GitHub Actions のIPを拒否するため。
- 以降: 決算短信を評価するたびに、短信XBRLから復元した当四半期を追記(source=tdnet)。Actions だけで回る。
出典: IRBANK(元データは TDnet / EDINET)。再配信時は出典の明示が必要。
"""
import logging
from pathlib import Path

import pandas as pd

from .config import DATA_DIR
from .datasources import irbank

log = logging.getLogger(__name__)

COLUMNS = ["code", "end", "sales", "op", "ordinary", "net", "announced", "source"]
FIELDS = ["sales", "op", "ordinary", "net"]


def path(data_dir: Path = DATA_DIR) -> Path:
    return data_dir / "quarterly_history.csv"


def load(data_dir: Path = DATA_DIR) -> pd.DataFrame:
    p = path(data_dir)
    if not p.exists():
        return pd.DataFrame(columns=COLUMNS)
    return pd.read_csv(p, dtype={"code": str, "end": str, "announced": str, "source": str},
                       keep_default_na=False).reindex(columns=COLUMNS)


def save(df: pd.DataFrame, data_dir: Path = DATA_DIR) -> None:
    df = df.copy()
    for f in FIELDS:
        df[f] = pd.to_numeric(df[f], errors="coerce").round(0).astype("Int64")
    df.sort_values(["code", "end"]).to_csv(path(data_dir), index=False, encoding="utf-8")


def by_code(df: pd.DataFrame) -> dict[str, list[dict]]:
    """code -> [{end, sales, op, ordinary, net, announced}, ...](古い順)。金額は float(円)、欠損は None。"""
    out: dict[str, list[dict]] = {}
    for r in df[df["end"] != ""].sort_values(["code", "end"]).to_dict("records"):
        out.setdefault(r["code"], []).append({
            "end": r["end"], "announced": r["announced"],
            **{f: (None if pd.isna(r[f]) or r[f] == "" else float(r[f])) for f in FIELDS},
        })
    return out


def merge(df: pd.DataFrame, rows: list[dict]) -> pd.DataFrame:
    """(code, end) が重なる場合は新しい行で置き換える。"""
    if not rows:
        return df
    new = pd.DataFrame(rows).reindex(columns=COLUMNS)
    key = lambda d: d["code"] + "|" + d["end"]  # noqa: E731
    return pd.concat([df[~key(df).isin(set(key(new)))], new], ignore_index=True)


def bootstrap(cfg: dict, codes: list[str], data_dir: Path = DATA_DIR, years: int = 3,
              checkpoint_every: int = 100) -> dict:
    """IRBANK から過去 years 年分を取得(手元のPCで実行)。取得済みの銘柄は飛ばすので中断しても再開できる。"""
    session = irbank.session_from_config(cfg)
    df = load(data_dir)
    have = set(df.loc[df["source"] == "irbank", "code"])
    todo = [c for c in codes if c not in have]
    n_ok = n_empty = 0
    buf: list[dict] = []
    for i, code in enumerate(todo, 1):
        try:
            q = irbank.fetch_quarterly(session, code)
        except Exception as e:
            log.warning("IRBANK取得失敗 %s: %s", code, e)
            continue
        q = q[-years * 4:]
        if not q:
            n_empty += 1
            # 空でも「取得済み」と分かるように印を残す(再開時に飛ばす)
            buf.append({"code": code, "end": "", "source": "irbank"})
        for r in q:
            buf.append({"code": code, "end": r["end"], **{f: r[f] for f in FIELDS},
                        "announced": r["announced"], "source": "irbank"})
        n_ok += bool(q)
        if i % checkpoint_every == 0 or i == len(todo):
            df = merge(df, buf)
            save(df, data_dir)
            buf = []
            log.info("履歴 %d/%d (取得 %d, 空 %d)", i, len(todo), n_ok, n_empty)
    return {"fetched": n_ok, "empty": n_empty, "skipped": len(codes) - len(todo), "rows": len(load(data_dir))}
