"""TDnet に決算説明資料が出ていない決算について、各社IRサイトから説明資料PDFを探して data/ir_docs.csv に記録する。

- 短信の「決算補足説明資料作成の有無」が「有」の会社だけ探す(「無」は資料が存在しない: status=no_material)

- 会社のURLは決算短信XBRLの「URL」欄(data/company_urls.csv にためる)
- 見つからなければ retry_minutes ごとに max_attempts 回まで探し直す(説明資料は短信より後に載ることが多い)
- watch の1分ごとの確認の合間に、time_budget_sec 秒だけ進める
"""
import logging
import queue
import threading
import time
from datetime import date
from pathlib import Path

import pandas as pd

from . import disclosures
from .config import DATA_DIR, now_jst
from .datasources import irsite, tdnet
from .datasources.http import RateLimitedSession

log = logging.getLogger(__name__)

COLUMNS = ["disclosure_id", "code", "material", "status", "url", "title", "score", "attempts", "checked_at"]


def path(data_dir: Path = DATA_DIR) -> Path:
    return data_dir / "ir_docs.csv"


def urls_path(data_dir: Path = DATA_DIR) -> Path:
    return data_dir / "company_urls.csv"


def load(data_dir: Path = DATA_DIR) -> pd.DataFrame:
    p = path(data_dir)
    if not p.exists():
        return pd.DataFrame(columns=COLUMNS)
    return pd.read_csv(p, dtype=str, keep_default_na=False).reindex(columns=COLUMNS, fill_value="")


def found_by_disclosure(data_dir: Path = DATA_DIR) -> dict[str, dict]:
    df = load(data_dir)
    return {r["disclosure_id"]: r for r in df[df["status"] == "found"].to_dict("records")}


def _load_urls(data_dir: Path) -> dict[str, str]:
    p = urls_path(data_dir)
    if not p.exists():
        return {}
    df = pd.read_csv(p, dtype=str, keep_default_na=False)
    return dict(zip(df["code"], df["url"]))


def _save_urls(urls: dict[str, str], data_dir: Path) -> None:
    pd.DataFrame(sorted(urls.items()), columns=["code", "url"]).to_csv(urls_path(data_dir), index=False, encoding="utf-8")


def targets(disc: pd.DataFrame, docs: pd.DataFrame, cfg: dict, today: date) -> pd.DataFrame:
    """探す対象: 直近 lookback_days 日の決算短信で、TDnet に説明資料が無く、まだ見つかっていないもの。"""
    icfg = cfg["irdocs"]
    earn = disc[(disc["kind"] == "earnings_report") & (disc["is_correction"] != "True")].copy()
    earn["d"] = earn["disclosed_at"].str[:10]
    earn = earn[earn["d"] >= (today - pd.Timedelta(days=icfg["lookback_days"])).isoformat()]
    pres = disc[disc["kind"] == "earnings_presentation"]
    pres_by_code = pres.groupby("code")["disclosed_at"].apply(list).to_dict()

    def has_tdnet_pres(r):
        d0 = date.fromisoformat(r["d"])
        return any(0 <= (date.fromisoformat(x[:10]) - d0).days <= 14 for x in pres_by_code.get(r["code"], []))

    earn = earn[[not has_tdnet_pres(r) for r in earn.to_dict("records")]]
    st = docs.set_index("disclosure_id")
    now = now_jst()
    keep = []
    for r in earn.to_dict("records"):
        if r["disclosure_id"] in st.index:
            x = st.loc[r["disclosure_id"]]
            if x["status"] in ("found", "none", "no_material") or int(x["attempts"] or 0) >= icfg["max_attempts"]:
                continue
            last = pd.Timestamp(x["checked_at"]).tz_localize(now.tzinfo) if x["checked_at"] else None
            if last is not None and (now - last).total_seconds() < icfg["retry_minutes"] * 60:
                continue
        keep.append(r)
    return pd.DataFrame(keep, columns=list(earn.columns)).sort_values("disclosed_at", ascending=False)


def _search(cfg: dict, jobs: list[dict], render: bool, deadline: float, on_result) -> None:
    """jobs を workers 本のスレッドで探す。スレッドごとに別セッション・別ブラウザ(同じサイトへは1秒に1回まで)。
    Playwright は作ったスレッドでしか使えない/閉じられないので、各スレッドが最後に自分で閉じる。"""
    icfg, h = cfg["irdocs"], cfg["http"]
    q: queue.Queue = queue.Queue()
    for jb in jobs:
        q.put(jb)
    lock = threading.Lock()
    from .site import period_label          # 循環 import を避ける

    def worker():
        site = irsite.IRSite(RateLimitedSession(h["user_agent"], h["min_interval_sec"], 1, 15), h["user_agent"])
        try:
            while time.monotonic() < deadline:
                try:
                    jb = q.get_nowait()
                except queue.Empty:
                    return
                try:
                    res = irsite.find_presentation(
                        site, jb["home"], date.fromisoformat(jb["disclosed_at"][:10]), period_label(jb),
                        irsite.fy_label_of(jb["title"]),
                        icfg["render_max_pages"] if render else icfg["max_pages"], render=render)
                except Exception as e:
                    log.warning("IRサイト探索失敗 %s %s: %s", jb["code"], jb["home"], e)
                    res = None
                with lock:
                    on_result(jb, res)
        finally:
            site.close()

    n = min(icfg["render_workers"] if render else icfg["workers"], max(1, len(jobs)))
    threads = [threading.Thread(target=worker, daemon=True) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def update(cfg: dict, data_dir: Path = DATA_DIR, time_budget_sec: float | None = None,
           limit: int | None = None) -> dict:
    """1) 短信XBRLで「決算補足説明資料作成の有無」と会社URLを確認 2) 「有」の会社をIRサイトで並列に探す
    3) 見つからなければブラウザ表示で探し直す。checkpoint_every 件ごとに保存する。"""
    icfg = cfg["irdocs"]
    budget = icfg["time_budget_sec"] if time_budget_sec is None else time_budget_sec
    t0 = time.monotonic()
    deadline = t0 + budget
    today = now_jst().date()
    disc = disclosures.load(data_dir)
    docs = load(data_dir)
    todo = targets(disc, docs, cfg, today)
    if limit:
        todo = todo.head(limit)
    if todo.empty:
        return {"checked": 0, "found": 0, "remaining": 0}

    urls = _load_urls(data_dir)
    prev = docs.set_index("disclosure_id")["attempts"].to_dict()
    prev_material = docs.set_index("disclosure_id")["material"].to_dict()
    results: dict[str, dict] = {}
    stamp = lambda: now_jst().strftime("%Y-%m-%dT%H:%M:%S")  # noqa: E731

    def record(j: dict, status: str, res: dict | None = None):
        results[j["disclosure_id"]] = {
            "disclosure_id": j["disclosure_id"], "code": j["code"], "material": j["material"], "status": status,
            "url": res["url"] if res else "", "title": res["title"] if res else "",
            "score": res["score"] if res else "", "attempts": j["attempts"], "checked_at": stamp()}
        if len(results) % icfg["checkpoint_every"] == 0:
            save()

    def save():
        nonlocal docs
        if not results:
            return
        new = pd.DataFrame(list(results.values())).reindex(columns=COLUMNS)
        docs = pd.concat([docs[~docs["disclosure_id"].isin(new["disclosure_id"])], new])
        docs.sort_values("checked_at", ascending=False).to_csv(path(data_dir), index=False, encoding="utf-8")
        _save_urls(urls, data_dir)

    # 1) 有無と会社URL(TDnet は1秒に1回まで、初回だけ)
    tdnet_s = RateLimitedSession.from_config(cfg)
    jobs = []
    for r in todo.to_dict("records"):
        if time.monotonic() > deadline:
            break
        material = prev_material.get(r["disclosure_id"], "")
        if (not material or r["code"] not in urls) and r["xbrl_url"]:
            try:
                x = tdnet.fetch_earnings(tdnet_s, r["xbrl_url"])
            except Exception as e:
                log.warning("XBRL取得失敗 %s: %s", r["code"], e)
                x = {}
            material = material or {True: "有", False: "無"}.get(x.get("has_material"), "")
            if x.get("company_url"):
                urls[r["code"]] = x["company_url"]
        j = {**r, "material": material, "home": urls.get(r["code"], ""),
             "attempts": int(prev.get(r["disclosure_id"]) or 0) + 1}
        if material == "無":
            record(j, "no_material")
        elif not j["home"]:
            record(j, "none")
        else:
            jobs.append(j)

    def done(j, res):
        if res:
            record(j, "found", res)
        elif not (icfg.get("render") and j["material"] == "有"):
            record(j, "none" if j["attempts"] >= icfg["max_attempts"] else "retry")

    # 2) 普通に読んで探す(並列)
    _search(cfg, jobs, False, deadline, done)
    # 3) 「有」で見つからなかった会社はブラウザで表示して探し直す
    if icfg.get("render"):
        rest = [j for j in jobs if j["disclosure_id"] not in results and j["material"] == "有"]

        def done_render(j, res):
            record(j, "found" if res else ("none" if j["attempts"] >= icfg["max_attempts"] else "retry"), res)
        _search(cfg, rest, True, deadline, done_render)
    save()
    found = sum(r["status"] == "found" for r in results.values())
    stats = {"checked": len(results), "found": found, "remaining": len(todo) - len(results)}
    log.info("irdocs: %s", stats)
    return stats
