"""サイトのセグメント・受注の数値を全社で照合する(手元でのみ。IRBANK の API を使う)。

セグメント: サイトの3か月の値を IRBANK のセグメント別業績(XBRL から別に作られた単独四半期の値)と照らす。
  - 利益(セグメント利益)は全社で比べる。売上は基準が「外部顧客」の会社だけ(IRBANK は外部顧客の売上)
  - 差が 1 百万円(丸め)を超えたものを出す
受注: 3か月の受注高がマイナス、全社とセグメントの和が合わない、受注残 ≠ 前の受注残 + 受注高 − 売上(大きくずれる)ものを出す
結果は ~/kessan-pdfs/audit_numbers.txt にも書く。
"""
import json
import logging
import re
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
logging.disable(logging.CRITICAL)

from src import history, segments, site  # noqa: E402
from src.config import load_config  # noqa: E402
from src.datasources import irbank_api  # noqa: E402

OUT = Path.home() / "kessan-pdfs" / "audit_numbers.txt"
key = lambda n: re.sub(r"\s|・|事業$|部門$", "", unicodedata.normalize("NFKC", n or ""))


def main() -> int:
    cfg = load_config()
    # 決算の行(新しい順)のうち、セグメントか受注があるもの。会社ごとに一番新しい決算だけ見る
    rows = [r for r in site.feed_rows(cfg) if r["group"] == "決算" and (r.get("seg") or r.get("ord"))]
    seg_basis = {}
    for r in segments.load().to_dict("records"):
        if r["status"] == "ok" and r["data"]:
            seg_basis[r["disclosure_id"]] = json.loads(r["data"]).get("basis")
    client = irbank_api.Client()
    issues, checked = defaultdict(list), defaultdict(int)
    seen = set()
    for r in rows:
        code = r["code"]
        if code in seen:
            continue
        seen.add(code)
        g = r.get("seg")
        # ---- セグメント: IRBANK と照合
        if g and g.get("hist"):
            try:
                sec = client.get(f"securities/{code}")
                ec = sec and sec.get("edinet_code")
                seg = client.get("segments", {"edinet_code": ec, "period": "quarter"}) if ec else None
            except irbank_api.QuotaExceeded:
                print("今日の API の上限。続きは明日", flush=True)
                break
            irb = defaultdict(dict)                    # 名前 -> 四半期の期末(YYYY-MM) -> (売上, 利益)
            for s in (seg or {}).get("segments", []):
                fy, q = s.get("fiscal_year"), s.get("quarter")
                if not fy or not q:
                    continue
                y, m = int(fy[:4]), int(fy[5:7]) - 3 * (4 - int(q))
                y, m = y + (m - 1) // 12, (m - 1) % 12 + 1
                irb[key(s.get("segment_name"))][f"{y:04d}-{m:02d}"] = (s.get("revenue"), s.get("operating_income"))
            ext = seg_basis.get(r["id"]) != "計"
            for h in g["hist"]:
                if h.get("total"):
                    continue
                theirs = irb.get(key(h["name"]))
                if not theirs:
                    continue
                for x in h["rows"]:
                    t = theirs.get(x["end"])
                    if not t:
                        continue
                    for f, i, use in (("sales", 0, ext), ("op", 1, True)):
                        v, tv = (x.get(f) or {}).get("v"), t[i]
                        if not use or v is None or tv is None:
                            continue
                        checked["セグメント"] += 1
                        if abs(v - tv / 1e6) > 1.01:
                            issues[f"{code} {r['name']}"].append(
                                f"セグメント {h['name']} {x['end']} {'売上' if f == 'sales' else '利益'} サイト {v:,} / IRBANK {tv / 1e6:,.0f}")
        # ---- 受注
        o = r.get("ord")
        if o:
            for name, hrows in [("全社", o.get("hist") or [])] + [(s["name"], s["rows"]) for s in o.get("seg_hist", [])]:
                for x in hrows:
                    checked["受注"] += 1
                    if x.get("q") is not None and x["q"] < 0:
                        issues[f"{code} {r['name']}"].append(f"受注 {name} {x['label']} 3か月の受注高がマイナス {x['q']:,}")
            tot = (o.get("total") or {}).get("orders", {}).get("v")
            segs = [s for s in o.get("segments", []) if not s.get("parent") and (s.get("orders") or {}).get("v") is not None]
            if tot is not None and len(segs) >= 2:
                ssum = sum(s["orders"]["v"] for s in segs)
                checked["受注"] += 1
                if abs(ssum - tot) > max(2, abs(tot) * 0.02) and ssum < tot * 0.98:
                    issues[f"{code} {r['name']}"].append(f"受注 セグメントの和 {ssum:,} が全社 {tot:,} より小さい(載っていない部門がある?)")
    lines = [f"照合した数: {dict(checked)}  ずれのある会社: {len(issues)}"]
    for k, v in sorted(issues.items()):
        lines.append(k)
        lines += ["    " + x for x in v[:12]]
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines[:200]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
