"""決算説明資料の「前年 / 今年 / 増減額 / 増減率」の表から、全社の受注高・受注残高を読む(短信に受注が無い会社だけ)。

読むのは、次の条件をすべて満たす行だけ(数値を間違えて出さないため):
  - 「受注高 前年 今年 増減額 増減率」の形で、増減額 = 今年 − 前年(端数の ±1 まで)
  - 表の上に単位(百万円・千円・億円)が書いてある
  - 期間が累計とわかる(1Q・通期、または「累計」「中間」「上期」の表記)
  - 同じ表の売上高の行が、その期の連結売上(短信)と一致する(全社の表で、同じ期間だと確かめる)
読んだ値は、その説明資料と同じ日の決算短信の記録として data/orders.csv に入れる(src="presentation")。
"""
import io
import json
import logging
import re
import unicodedata
from pathlib import Path

import pandas as pd

from . import disclosures, earnings, orders
from .config import DATA_DIR
from .datasources import tdnet

log = logging.getLogger(__name__)

PDF_DIR = Path.home() / "kessan-pdfs"
_UNITS = {"百万円": 1e6, "千円": 1e3, "億円": 1e8}
_N = r"[△▲\-－+＋]?\d[\d,]*(?:\.\d+)?"
_SALES = re.compile(r"^(?:売上高|売上収益|営業収益)\s+(?P<a>" + _N + r")\s+(?P<b>" + _N + r")\s+(?P<d>" + _N + r")\s+(?P<r>" + _N + r")%\s*$")
_ROW = re.compile(r"^(?P<lab>受注高|受注額|受注残高?|繰越工事高(?:\(受注残\))?|受注残高?\(繰越\))[^\d△▲\-－+＋]{0,20}?\s+"
                  r"(?P<a>" + _N + r")\s+(?P<b>" + _N + r")\s+(?P<d>" + _N + r")\s+(?P<r>" + _N + r")%\s*$")


def _num(t: str) -> float:
    t = t.replace(",", "").replace("＋", "+").replace("－", "-").replace("▲", "-").replace("△", "-")
    return float(t)


def text_of(path: Path) -> str:
    """PDF の文字(pdfium)。.txt があればそれ"""
    txt = path.with_suffix(".txt")
    if txt.exists():
        return txt.read_text(encoding="utf-8")
    import pypdfium2 as pdfium
    try:
        d = pdfium.PdfDocument(str(path))
        t = "\n".join(d[i].get_textpage().get_text_range() for i in range(len(d))).replace("\r\n", "\n").replace("\r", "\n")
    except Exception:
        t = ""
    try:
        txt.write_text(t, encoding="utf-8")
    except OSError:
        pass
    return t


def parse_text(text: str, n_q: int | None) -> dict | None:
    """説明資料の文字から、検算の通った最初の「受注高」と「受注残高」の行を読む。
    返り値: {"orders": 円, "orders_yoy": %, "backlog": 円, "backlog_yoy": %}(読めた方だけ)。読めなければ None"""
    pages = text.split("\f") if "\f" in text else [text]
    out = {}
    for page in pages:
        lines = [unicodedata.normalize("NFKC", l).strip() for l in page.splitlines()]
        lines = [re.sub(r"([△▲\-+])\s+(\d)", r"\1\2", l) for l in lines if l]
        for i, l in enumerate(lines):
            m = _ROW.match(l)
            if not m:
                continue
            a, b, d = _num(m["a"]), _num(m["b"]), _num(m["d"])
            if abs((b - a) - d) > 1.01:                # 検算: 増減額 = 今年 − 前年
                continue
            above = " ".join(lines[max(0, i - 25):i])
            mu = re.findall(r"単位\s*[:：]?\s*(百万円|千円|億円)", above) or                 re.findall(r"単位\s*[:：]?\s*(百万円|千円|億円)", " ".join(lines[i + 1:i + 9]))[:1]   # 表のすぐ下に書く会社も
            if not mu:
                continue
            cum = n_q in (1, 4) or re.search(r"累計|中間|上期|上半期|通期", above)
            if not cum or re.search(r"3か月|3ヶ月|四半期会計期間", above):
                continue
            unit = _UNITS[mu[-1]]
            key = "orders" if m["lab"] in ("受注高", "受注額") else "backlog"
            if key in out:
                continue
            # 同じ表(前後 12 行)の売上高の行(同じ形・検算つき)。全社の表かどうかを短信の連結売上と照らすのに使う
            sales = None
            for l2 in lines[max(0, i - 12):i + 13]:
                ms = _SALES.match(l2)
                if ms and abs((_num(ms["b"]) - _num(ms["a"])) - _num(ms["d"])) <= 1.01:
                    sales = _num(ms["b"]) * unit
                    break
            if sales is None:
                continue
            out[key] = b * unit
            out[f"{key}_yoy"] = None if a == 0 else round((b / a - 1) * 100, 1)
            out[f"{key}_sales"] = sales
    return out or None


def matches_consolidated(x: dict, code: str, ym: str, n_q: int, hist: dict) -> dict | None:
    """読んだ受注の表の売上高が、その期の連結売上(短信の四半期の値の累計)と単位の丸めの範囲で一致するものだけ残す"""
    qs = [hist.get(code, {}).get(orders._ym_shift(ym, -3 * k)) for k in range(n_q)]
    if any(q is None or q.get("sales") is None for q in qs):
        return None
    cons = sum(q["sales"] for q in qs)
    out = {}
    for key in ("orders", "backlog"):
        s = x.get(f"{key}_sales")
        unit = 1e8 if s and s % 1e8 == 0 and abs(s - cons) < 1e8 else 1e6
        if s is not None and abs(s - cons) <= max(unit, abs(cons) * 0.002):
            out[key], out[f"{key}_yoy"] = x[key], x[f"{key}_yoy"]
    return out or None


def _record(code: str, did: str, disclosed_at: str, n_q, period_end, x: dict, pres_doc: str) -> dict:
    total = {"name": "合計", "orders": x.get("orders"), "orders_yoy": x.get("orders_yoy"),
             "backlog": x.get("backlog"), "backlog_yoy": x.get("backlog_yoy"), "backlog_fy": None}
    data = {"total": total, "segments": [], "src": "presentation", "pres_doc": pres_doc}
    return {"disclosure_id": did, "code": code, "disclosed_at": disclosed_at, "n_q": str(n_q), "period_end": period_end,
            "status": "ok", "data": json.dumps(data, ensure_ascii=False)}


def _skip_codes(df: pd.DataFrame) -> set[str]:
    """短信の表や本文から受注を読めている会社(短信を優先する)"""
    ok = df[df["status"] == "ok"]
    return {r["code"] for r in ok.to_dict("records") if '"src": "presentation"' not in r["data"]}


def build_from_local(data_dir: Path = DATA_DIR) -> int:
    """手元の過去2年分の説明資料(~/kessan-pdfs)から。説明資料と同じ日(〜3日前)の決算短信の記録にする"""
    from .datasources import irbank_api
    df = orders.load(data_dir)
    skip, have = _skip_codes(df), set(df[df["status"] == "ok"]["disclosure_id"])
    idx = irbank_api.load_index()
    idx = idx[(idx["doc_id"] != "") & (idx["source"] == "tdnet")]
    kind = idx["title"].map(lambda t: tdnet.classify_title(t))
    idx = idx.assign(kind=kind.map(lambda c: c["kind"]), corr=kind.map(lambda c: c["is_correction"]))
    reps = idx[(idx["kind"] == "earnings_report") & ~idx["corr"]]
    pres = idx[idx["kind"] == "earnings_presentation"]
    from . import history
    hist = {c: {q["end"]: q for q in v} for c, v in history.by_code(history.load(data_dir)).items()}
    rows = []
    for p in pres.to_dict("records"):
        if p["code"] in skip:
            continue
        pdf = PDF_DIR / p["code"] / f"{p['doc_id']}.pdf"
        if not pdf.exists():
            continue
        r = reps[(reps["code"] == p["code"]) & (reps["filing_date"] <= p["filing_date"])]
        r = r[r["filing_date"] >= (pd.Timestamp(p["filing_date"]) - pd.Timedelta(days=3)).strftime("%Y-%m-%d")]
        if r.empty:
            continue
        rep = r.sort_values("filing_date").iloc[-1]
        did = "1401" + rep["doc_id"]
        if did in have:
            continue
        n_q, ym = orders._quarter_of_title(rep["title"]), orders.period_end_of_title(rep["title"])
        x = parse_text(text_of(pdf), n_q)
        x = x and ym and matches_consolidated(x, p["code"], ym, n_q, hist)
        if not x:
            continue
        rows.append(_record(p["code"], did, f"{rep['filing_date']}T00:00", n_q, ym, x, p["doc_id"]))
        have.add(did)
    if rows:
        out = pd.concat([df[~df["disclosure_id"].isin([r["disclosure_id"] for r in rows])],
                         pd.DataFrame(rows).reindex(columns=orders.COLUMNS)])
        out.sort_values(["disclosed_at", "disclosure_id"], ascending=False).to_csv(orders.path(data_dir), index=False,
                                                                                 encoding="utf-8")
    log.info("説明資料の受注(手元): %d", len(rows))
    return len(rows)


def update(cfg: dict, data_dir: Path = DATA_DIR) -> int:
    """TDnet に出た新しい説明資料から(同じ日の短信に受注が無い会社だけ)。GitHub Actions でも動く"""
    from .datasources.http import RateLimitedSession
    disc = disclosures.load(data_dir)
    df = orders.load(data_dir)
    skip = _skip_codes(df)
    have = set(df[df["status"] == "ok"]["disclosure_id"])
    done = set(df[df["data"].str.contains('"pres_doc"', na=False)]["data"].str.extract(r'"pres_doc": "(\d+)"')[0].dropna())
    arch = {a["disclosure_id"]: a for a in earnings.load_archive(data_dir).to_dict("records")}
    reps = disc[(disc["kind"] == "earnings_report") & (disc["is_correction"] != "True")]
    pres = disc[disc["kind"] == "earnings_presentation"].sort_values("disclosed_at").tail(300)
    from . import history
    hist = {c: {q["end"]: q for q in v} for c, v in history.by_code(history.load(data_dir)).items()}
    session = None
    rows = []
    checked = _load_checked(data_dir)                  # 一度読んだ説明資料は(受注が無くても)読み直さない
    n_checked = len(checked)
    for p in pres.to_dict("records"):
        if p["code"] in skip or p["disclosure_id"][4:] in done or p["disclosure_id"] in checked:
            continue
        r = reps[(reps["code"] == p["code"]) & (reps["disclosed_at"].str[:10] <= p["disclosed_at"][:10])]
        r = r[r["disclosed_at"].str[:10] >= (pd.Timestamp(p["disclosed_at"][:10]) - pd.Timedelta(days=3)).strftime("%Y-%m-%d")]
        if r.empty:
            continue
        rep = r.sort_values("disclosed_at").iloc[-1]
        if rep["disclosure_id"] in have:
            continue
        a = arch.get(rep["disclosure_id"]) or {}
        if not a.get("n_q") or not a.get("period_end"):
            continue
        session = session or RateLimitedSession.from_config(cfg)
        try:
            resp = session.get(p["pdf_url"])
        except Exception as e:
            log.warning("説明資料の取得失敗 %s: %s", p["code"], e)
            continue
        checked.add(p["disclosure_id"])
        if resp is None:
            continue
        import pypdfium2 as pdfium
        try:
            d = pdfium.PdfDocument(resp.content)
            text = "\n".join(d[i].get_textpage().get_text_range() for i in range(len(d))).replace("\r\n", "\n")
        except Exception:
            continue
        n_q = int(float(a["n_q"]))
        x = parse_text(text, n_q)
        x = x and matches_consolidated(x, p["code"], str(a["period_end"])[:7], n_q, hist)
        if x:
            rows.append(_record(p["code"], rep["disclosure_id"], rep["disclosed_at"], n_q, str(a["period_end"])[:7], x,
                                p["disclosure_id"][4:]))
            have.add(rep["disclosure_id"])
    if rows:
        out = pd.concat([df[~df["disclosure_id"].isin([r["disclosure_id"] for r in rows])],
                         pd.DataFrame(rows).reindex(columns=orders.COLUMNS)])
        out.sort_values(["disclosed_at", "disclosure_id"], ascending=False).to_csv(orders.path(data_dir), index=False,
                                                                                 encoding="utf-8")
    if len(checked) != n_checked:
        _checked_path(data_dir).write_text("".join(f"{x}\n" for x in sorted(checked)), encoding="utf-8")
    log.info("説明資料の受注: %d", len(rows))
    return len(rows)


def _checked_path(data_dir: Path) -> Path:
    return data_dir / "pres_checked.txt"


def _load_checked(data_dir: Path) -> set[str]:
    p = _checked_path(data_dir)
    return set(p.read_text(encoding="utf-8").split()) if p.exists() else set()
