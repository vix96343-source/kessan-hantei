"""各社IRサイトの IR カレンダーから決算発表の予定時刻を読む → data/ir_times.csv(手元のみ・local_backfill から)

JPX の予定日には時刻が無く、前回の短信の時刻は目安でしかない(変える会社が約2割)。
会社が IR カレンダーに「2026年10月30日 15:00 決算発表」のように書いていればそれを使う。

- 会社URL: data/company_urls.csv(短信XBRL)。無ければ前回の短信PDF(IRBANK 保管)の表紙の「URL」欄から取って足す。
- たどるページ: トップ → IR トップ → カレンダー・スケジュール系(静的に無ければブラウザ表示も)。robots.txt を守り1秒に1回まで。
- 読み方: 予定日の日付がある行(とその前後の行)に「決算」があり、説明会の時刻ではない時刻があれば採用。
  日付はあるが時刻が無いときは status=no_time(時刻は書かれていない)。
"""
import io
import logging
import re
import time
import unicodedata
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup

from . import announce_times, calendar_fetch, disclosures, irdocs
from .config import DATA_DIR, now_jst
from .datasources import irbank_api
from .datasources.irsite import IR_LINK, IRSite, _links, _same_site
from .datasources.http import RateLimitedSession

log = logging.getLogger(__name__)

COLUMNS = ["announce_date", "code", "status", "ir_time", "source_url", "snippet", "checked_at"]
CAL_LINK = re.compile(r"カレンダー|calendar|スケジュール|schedule|決算発表予定|IRイベント", re.I)
IR_PATH = re.compile(r"(^|/)(ir|investors?|kabunushi|stock|shareholders?)(/|$|[._-])", re.I)
SKIP_EXT = (".pdf", ".zip", ".xlsx", ".xls", ".doc", ".docx", ".ppt", ".pptx", ".mp4", ".mov", ".jpg", ".png", ".gif")
COMPANY_SEC = 60           # 1社あたりの上限秒数
RECHECK_HOURS = 20          # 同じ銘柄・日付を見直す間隔(時刻を変える会社があるので毎日1回は見直す)
MAX_PAGES = 8
BRIEFING = re.compile(r"説明会|電話会議|カンファレンス|ミーティング|コール|call|配信|中継|スモール|個人投資家", re.I)
EARNINGS = re.compile(r"決算|短信|業績発表|四半期|中間")
TIME = re.compile(r"(午前|午後)?\s*([0-2]?\d)\s*(?:[:：]\s*([0-5]\d)|時\s*(?:([0-5]?\d)\s*分|半)?)")
WEEKDAY = r"\s*[\(（][月火水木金土日][\)）]"


def path(data_dir: Path = DATA_DIR) -> Path:
    return data_dir / "ir_times.csv"


def load(data_dir: Path = DATA_DIR) -> pd.DataFrame:
    p = path(data_dir)
    if not p.exists():
        return pd.DataFrame(columns=COLUMNS)
    return pd.read_csv(p, dtype=str, keep_default_na=False).reindex(columns=COLUMNS, fill_value="")


def norm(s: str) -> str:
    return unicodedata.normalize("NFKC", s or "")


# ---------------------------------------------------------------- ページ → 予定日の項目

ANY_DATE = re.compile(r"20\d{2}\s*[年/.\-]\s*\d{1,2}\s*[月/.\-]\s*\d{1,2}|(?<![\d/])\d{1,2}\s*月\s*\d{1,2}\s*日"
                      r"|(?<![\d/])\d{1,2}/\d{1,2}\s*[\(（][月火水木金土日]")
VAGUE = re.compile(r"^\s*(?:台|以降|以後)")


def text_of(el) -> str:
    return re.sub(r"\s+", " ", norm(el.get_text(" ") if hasattr(el, "get_text") else str(el))).strip()


def date_pattern(d: date) -> re.Pattern:
    """予定日の書き方(2026年10月30日 / 2026/10/30 / 2026.10.30 / 10月30日 / 10/30(金))"""
    y, m, dd = d.year, d.month, d.day
    mm, ddd = rf"0?{m}", rf"0?{dd}"
    return re.compile(rf"(?<![\d/])(?:{y}\s*年\s*{mm}\s*月\s*{ddd}\s*日|{y}\s*[/.\-]\s*{mm}\s*[/.\-]\s*{ddd}(?!\d)"
                      rf"|{mm}\s*月\s*{ddd}\s*日|{mm}\s*/\s*{ddd}{WEEKDAY})")


def _hhmm(m: re.Match, after: str) -> str | None:
    """時刻の表記 → HH:MM。「11時台」「15時以降」のような幅のある書き方は使わない"""
    if VAGUE.match(after):
        return None
    ampm, h, mi1, mi2 = m.group(1), int(m.group(2)), m.group(3), m.group(4)
    mi = int(mi1 or mi2 or (30 if "半" in m.group(0) else 0))
    if ampm == "午後" and h < 12:
        h += 12
    if not 7 <= h <= 21:
        return None
    return f"{h:02d}:{mi:02d}"


def items(soup: BeautifulSoup, d: date) -> list[str]:
    """予定日が書かれた項目(表の行・箇条書きの1項目・dt と dd の組など)の文字。
    日付の入った一番内側の要素から外へ広げ、「決算」が入った時点の範囲を1項目とする(別の日付が入ったら打ち切り)。"""
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    pat = date_pattern(d)
    out, seen = [], set()
    day = re.compile(rf"(?<!\d)0?{d.day}(?!\d)")
    for s in soup.find_all(string=lambda x: bool(x and day.search(norm(x)))):
        el = s.parent
        for _ in range(3):                                         # 日付が複数の要素に分かれている(10月 / 29日)
            if el is None or pat.search(text_of(el)):
                break
            el = el.parent
        if el is None or not pat.search(text_of(el)) or id(el) in seen:
            continue
        seen.add(id(el))
        cur, item = el, None
        for _ in range(6):
            txt = text_of(cur)
            if len(ANY_DATE.findall(txt)) > 1:
                break
            if EARNINGS.search(txt):
                item = txt
                break
            # dt と dd、見出しセルとセルのように、項目が兄弟要素に分かれている書き方
            pair = {"dt": "dd", "th": "td", "dd": "dt", "td": "th"}.get(cur.name)
            sib = None
            if pair:
                sib = (cur.find_next_sibling(pair) if cur.name in ("dt", "th") else cur.find_previous_sibling(pair))
            if sib is not None:
                both = txt + " / " + text_of(sib) if cur.name in ("dt", "th") else text_of(sib) + " / " + txt
                if len(ANY_DATE.findall(both)) <= 1 and EARNINGS.search(both):
                    item = both
                    break
            cur = cur.parent
            if cur is None or cur.name in ("body", "html", "[document]"):
                break
        if item and item not in out:
            out.append(item)
    return out


def time_in(item: str) -> str | None:
    """項目の中の決算発表の時刻。説明会などの部分は除き、違う時刻が2つ以上あれば決めない"""
    parts = [x for x in re.split(r"[/／、,，;；・]|\s{2,}", item) if x.strip()]
    times = []
    for x in parts:
        if BRIEFING.search(x):
            continue
        for m in TIME.finditer(x):
            t = _hhmm(m, x[m.end():])
            if t and t not in times:
                times.append(t)
    return times[0] if len(times) == 1 else None


def extract(soup: BeautifulSoup, d: date) -> dict | None:
    """ページから予定日の決算発表の時刻を探す。{status, ir_time, snippet} / 予定日の決算の項目が無ければ None"""
    best = None
    for item in items(soup, d):
        # 説明会だけの項目は飛ばす(「決算説明会」は決算を含むので、発表・短信が無ければ説明会の項目)
        if BRIEFING.search(item) and not re.search(r"発表|短信", item):
            continue
        t = time_in(item)
        cand = {"status": "found" if t else "no_time", "ir_time": t or "", "snippet": item[:200]}
        if t:
            return cand
        best = best or cand
    return best


# ---------------------------------------------------------------- 会社URL

def url_from_tanshin(code: str, data_dir: Path = DATA_DIR, ua: str = "kessan-hantei") -> str:
    """前回の短信PDF(IRBANK 保管)の表紙の「URL」欄。取れなければ空"""
    import pypdf
    idx = irbank_api.load_index(data_dir, code)
    if idx.empty:
        return ""
    er = announce_times._reports(idx[idx["source"] == "tdnet"]).sort_values("disclosed_at", ascending=False)
    for u in er["pdf_url"].head(2):
        try:
            b = requests.get(u, timeout=30, headers={"User-Agent": ua}).content
            text = norm(pypdf.PdfReader(io.BytesIO(b)).pages[0].extract_text())
        except Exception as e:
            log.debug("短信PDF %s: %s", u, e)
            continue
        m = re.search(r"URL\s*[:]?\s*((?:https?://)?[\w.-]+\.[a-z]{2,}(?:/[\w./%-]*)?)", text, re.I)
        if m:
            u = m.group(1).rstrip(".")
            return u if u.startswith("http") else "https://" + u
        time.sleep(1)
    return ""


# ---------------------------------------------------------------- IRサイト

def _soups(site: IRSite, url: str, render: bool) -> tuple[list[BeautifulSoup], list[tuple[str, str]]] | None:
    """ページ(ブラウザ表示なら iframe の中も)と、その中のリンク"""
    if not render:
        soup = site.page(url)
        return None if soup is None else ([soup], _links(soup))
    html = site.render_html(url)
    if not html:
        return None
    soups = []
    for h in html:
        soup = BeautifulSoup(h["html"], "html.parser")
        soup.base_url = h["url"]
        soups.append(soup)
    return soups, [lk for sp in soups for lk in _links(sp)]


def _extract_all(soups: list[BeautifulSoup], d: date) -> dict | None:
    best = None
    for sp in soups:
        r = extract(sp, d)
        if r and r["status"] == "found":
            return r
        best = best or r
    return best


def find(site: IRSite, home: str, d: date, render: bool = True) -> dict:
    """会社サイトから予定日の時刻を探す。{status: found/no_time/none, ir_time, source_url, snippet}"""
    if not home.startswith("http"):
        home = "https://" + home
    root = f"{urlparse(home).scheme}://{urlparse(home).netloc}"
    homes = [home]
    queue = [(0, home), (5, root + "/ir/"), (6, root + "/ir/calendar/"), (6, root + "/ir/schedule/")]
    seen, fetched, best = set(), 0, None
    rendered = []
    deadline = time.monotonic() + COMPANY_SEC
    while queue and fetched < MAX_PAGES and time.monotonic() < deadline:
        queue.sort(key=lambda x: x[0])
        pri, url = queue.pop(0)
        key = url.split("#")[0].rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        got = _soups(site, url, False)
        fetched += 1
        if got is None:
            continue
        soups, links = got
        r = _extract_all(soups, d)
        if r and r["status"] == "found":
            return {**r, "source_url": url}
        if r and best is None:
            best = {**r, "source_url": url}
        if pri <= 2:
            rendered.append(url)
        if pri not in (0, 2, 5):            # リンクをたどるのはトップと IR トップからだけ(カレンダーの先の行事ページへは行かない)
            continue
        for link, text in links:
            if link.lower().split("?")[0].endswith(SKIP_EXT):
                continue
            if pri == 0 and IR_LINK.search(text) and not any(_same_site(link, h) for h in homes):
                homes.append(f"{urlparse(link).scheme}://{urlparse(link).netloc}")
            if not any(_same_site(link, h) for h in homes) or link.split("#")[0].rstrip("/") in seen:
                continue
            path = urlparse(link).path
            in_ir = pri != 0 or IR_PATH.search(path) or IR_LINK.search(text)
            if (CAL_LINK.search(text) or CAL_LINK.search(path)) and in_ir:
                queue.append((1, link))
            elif pri == 0 and IR_LINK.search(text):
                queue.append((2, link))
    # 静的に読めなかったら、カレンダー・IRトップをブラウザで表示して(iframe の外部IRサービスも)読む
    if render:
        cal = [u for u in rendered if CAL_LINK.search(u)] or rendered[1:3]
        for url in cal[:2]:
            if time.monotonic() > deadline + 30:
                break
            got = _soups(site, url, True)
            if got is None:
                continue
            r = _extract_all(got[0], d)
            if r and r["status"] == "found":
                return {**r, "source_url": url}
            if r and best is None:
                best = {**r, "source_url": url}
    return best or {"status": "none", "ir_time": "", "snippet": "", "source_url": ""}


# ---------------------------------------------------------------- 一括

def targets(data_dir: Path = DATA_DIR, today: date | None = None) -> list[tuple[str, str]]:
    """まだ発表していない決算予定(今日以降)のうち、RECHECK_HOURS 以内に見ていないもの。日付の近い順"""
    today = today or now_jst().date()
    cal = calendar_fetch.load(data_dir)
    cal = cal[cal["announce_date"] >= today.isoformat()]
    done = set(announce_times.from_disclosures(disclosures.load(data_dir))[["code", "announce_date"]]
               .itertuples(index=False, name=None))
    cur = load(data_dir).set_index(["code", "announce_date"])[["status", "checked_at"]].to_dict("index")
    ago = lambda h: (now_jst() - pd.Timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M")  # noqa: E731
    limit, retry = ago(RECHECK_HOURS), ago(1)                 # 取得に失敗したものは1時間後に取り直す

    def due(key) -> bool:
        x = cur.get(key)
        return x is None or x["checked_at"] < (retry if x["status"] == "error" else limit)

    out = [(c, d) for c, d in zip(cal["code"], cal["announce_date"]) if (c, d) not in done and due((c, d))]
    return sorted(out, key=lambda x: (x[1], x[0]))


def update(cfg: dict, data_dir: Path = DATA_DIR, time_budget_sec: float = 300, codes: list[str] | None = None,
           render: bool = True) -> dict:
    h = cfg["http"]
    ua = h["user_agent"]
    jobs = targets(data_dir)
    if codes:
        jobs = [j for j in jobs if j[0] in codes]
    urls = irdocs._load_urls(data_dir)
    site = IRSite(RateLimitedSession(ua, max(1.0, h["min_interval_sec"]), 2, 15), ua)
    rows, deadline, stats = [], time.monotonic() + time_budget_sec, {"targets": len(jobs)}
    try:
        for code, d in jobs:
            if time.monotonic() > deadline:
                break
            if not urls.get(code):
                urls[code] = url_from_tanshin(code, data_dir, ua)
                if urls[code]:
                    irdocs._save_urls(urls, data_dir)
            home = urls.get(code, "")
            try:
                r = find(site, home, date.fromisoformat(d), render) if home else \
                    {"status": "no_url", "ir_time": "", "snippet": "", "source_url": ""}
            except Exception as e:
                log.warning("IRカレンダー %s %s: %s", code, d, e)
                r = {"status": "error", "ir_time": "", "snippet": str(e)[:200], "source_url": ""}
            rows.append({"announce_date": d, "code": code, **r, "checked_at": now_jst().strftime("%Y-%m-%dT%H:%M")})
            stats[r["status"]] = stats.get(r["status"], 0) + 1
    finally:
        site.close()
    if rows:
        new = pd.DataFrame(rows).reindex(columns=COLUMNS)
        old = load(data_dir)
        old = old[~old.set_index(["code", "announce_date"]).index.isin(new.set_index(["code", "announce_date"]).index)]
        out = pd.concat([old, new]).sort_values(["announce_date", "code"])
        out.to_csv(path(data_dir), index=False, encoding="utf-8")
    stats["checked"] = len(rows)
    log.info("ir_times: %s", stats)
    return stats
