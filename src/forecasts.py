"""判定(judge)用の「発表前に分かっていた会社予想」の補完 data/irbank_forecasts.csv。

TDnet の蓄積(tdnet_earnings.csv / revisions.csv)は運用開始(2026-09-08)以降の分しか無いので、
これから決算を出す銘柄の「前回の短信」「その後の業績予想の修正」が欠けていれば、IRBANK API の
xbrl_facts(TDnet の XBRL を IRBANK が保管したもの)から読んで足す。IRBANK は Actions から使えないので手元でのみ。
doc_kind: earnings = 決算短信、none = 探したが無い(同じ日に探し直さない印)
"""
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from .config import DATA_DIR, JST, now_jst
from .datasources import irbank_api, tdnet

log = logging.getLogger(__name__)

COLUMNS = ["disclosure_id", "code", "disclosed_at", "doc_kind", "n_q", "period_end", "scope",
           "cum_sales", "cum_op", "prior_sales", "prior_op", "fc_sales", "fc_op", "fc_q2_sales", "fc_q2_op",
           "fc_next_year", "revised", "fetched_at"]


def path(data_dir: Path = DATA_DIR) -> Path:
    return data_dir / "irbank_forecasts.csv"


def load(data_dir: Path = DATA_DIR) -> pd.DataFrame:
    p = path(data_dir)
    if not p.exists():
        return pd.DataFrame(columns=COLUMNS)
    return pd.read_csv(p, dtype={"disclosure_id": str, "code": str, "disclosed_at": str, "doc_kind": str,
                                 "period_end": str},
                       keep_default_na=False).reindex(columns=COLUMNS, fill_value="")


def _jst(iso_utc: str) -> str:
    """IRBANK の '2026-07-29T01:00:00.000Z' → JST 'YYYY-MM-DDTHH:MM'"""
    t = datetime.strptime(iso_utc[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    return t.astimezone(JST).strftime("%Y-%m-%dT%H:%M")


def _is_tanshin(title: str) -> bool:
    return "決算短信" in title and not any(w in title for w in ("訂正", "補足", "説明", "参考"))


def earnings_row(doc_id: str, code: str, disclosed_at: str, x: dict) -> dict:
    fc, cum, prior, q2 = x["forecast"], x["cum"], x["prior_cum"], x.get("forecast_q2") or {}
    return {"disclosure_id": "1401" + doc_id, "code": code, "disclosed_at": disclosed_at, "doc_kind": "earnings",
            "n_q": x["n_q"], "period_end": x["period_end"], "scope": x["scope"],
            "cum_sales": cum.get("sales"), "cum_op": cum.get("op"),
            "prior_sales": prior.get("sales"), "prior_op": prior.get("op"),
            "fc_sales": fc.get("sales"), "fc_op": fc.get("op"),
            "fc_q2_sales": q2.get("sales"), "fc_q2_op": q2.get("op"),
            "fc_next_year": bool(x.get("forecast_is_next_year")),
            "revised": "" if x.get("revised") is None else bool(x["revised"])}


def backfill(cfg: dict, targets: list[tuple[str, date]], have_snapshot: set[str],
             data_dir: Path = DATA_DIR, client=None) -> dict:
    """targets = [(code, 発表日)]。have_snapshot にある銘柄(前回の短信が手元にある)は飛ばす。
    期の初め(直近の本決算短信)から前回までの短信を IRBANK から読む。"""
    df = load(data_dir)
    today = now_jst().strftime("%Y-%m-%d")
    checked = set(df.loc[(df["doc_kind"] == "none") & (df["fetched_at"].str[:10] == today), "code"])
    have_ids = set(df["disclosure_id"])
    todo = [(c, d) for c, d in targets if c not in have_snapshot and c not in checked]
    if not todo:
        return {"targets": len(targets), "fetched": 0}
    client = client or irbank_api.Client(data_dir=data_dir)
    rows, n_docs = [], 0
    fresh_after = (now_jst() - timedelta(days=20)).strftime("%Y-%m-%dT%H:%M")
    for code, ann in todo:
        try:
            docs = irbank_api.earnings_reports(code, data_dir, fresh_after=fresh_after) or []
        except irbank_api.QuotaExceeded:
            log.warning("IRBANK の今日の上限に達したので中断")
            break
        before = sorted([d for d in docs if d.get("disclosed_at") and _jst(d["disclosed_at"]) < ann.isoformat()],
                        key=lambda d: d["disclosed_at"])
        tanshin = [d for d in before if _is_tanshin(d.get("title", ""))]
        at = now_jst().strftime("%Y-%m-%dT%H:%M")
        if not tanshin:
            rows.append({"disclosure_id": f"none-{code}", "code": code, "doc_kind": "none", "fetched_at": at})
            continue
        # 期の初め(直近の本決算短信)から前回までの短信。通期予想の推移から今期の修正の向きを数える
        # (IRBANK には業績予想の修正の XBRL 値が無いので、短信と短信の間の修正は次の短信の予想で拾う)
        last = tanshin[-1]
        annual = [d for d in tanshin if "四半期" not in d["title"] and "中間" not in d["title"]]
        since = annual[-1]["disclosed_at"] if annual else last["disclosed_at"]
        picks = [d for d in tanshin if d["disclosed_at"] >= since]
        for d in picks:
            did = "1401" + d["doc_id"]
            if did in have_ids:
                continue
            try:
                r = client.get(f"disclosures/{d['doc_id']}", {"include": "xbrl_facts"}) or {}
            except irbank_api.QuotaExceeded:
                log.warning("IRBANK の今日の上限に達したので中断")
                break
            except Exception as e:                    # 1件の失敗で止めない
                log.warning("IRBANK 開示取得失敗 %s %s: %s", code, d["doc_id"], e)
                continue
            n_docs += 1
            x = tdnet.parse_earnings_facts(r.get("xbrl_facts") or [])
            row = earnings_row(d["doc_id"], code, _jst(d["disclosed_at"]), x) if x and x.get("period_end") else None
            if row:
                rows.append({**row, "fetched_at": at})
                have_ids.add(did)
    if rows:
        new = pd.DataFrame(rows).reindex(columns=COLUMNS)
        out = pd.concat([df[~df["disclosure_id"].isin(new["disclosure_id"])], new])
        out.sort_values(["code", "disclosed_at", "disclosure_id"]).to_csv(path(data_dir), index=False,
                                                                         encoding="utf-8")
    stats = {"targets": len(targets), "todo": len(todo), "docs": n_docs, "rows": len(rows)}
    log.info("forecasts backfill: %s", stats)
    return stats
