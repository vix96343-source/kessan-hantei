"""各社IRサイトから決算説明資料のPDFを探す(TDnet に説明資料を出さない会社向け)。

会社のURL(決算短信XBRLに記載)→ IRページ → 決算資料・説明会・ライブラリのページ、と最大数ページたどり、
見つかったPDFを「資料らしさ」「期の一致」「発表日との近さ」で点数づけして一番良いものを返す。
robots.txt を守り、同じセッションで1秒に1回までに抑える。
"""
import logging
import re
import time
from datetime import date, timedelta
from urllib import robotparser
from urllib.parse import unquote, urljoin, urlparse

from bs4 import BeautifulSoup

from .http import RateLimitedSession

log = logging.getLogger(__name__)

IR_LINK = re.compile(r"(^|[^a-z])ir([^a-z]|$)|投資家|株主・投資家|株主の皆様|investor", re.I)
MATERIAL_LINK = re.compile(r"説明会|説明資料|プレゼン|presentation|IR資料|IRライブラリ|ライブラリ|library|決算資料|決算情報|"
                           r"決算関連|決算短信|決算説明|業績|financial|results|materials?", re.I)
EXTERNAL_IR_HOST = re.compile(r"eir-parts\.net|irpocket|magicalir|ir\.|xj-storage|irbank|ullet", re.I)

DOC_TYPE = re.compile(r"説明|プレゼン|presentation|業績概要|決算概要|決算の概要|決算資料|配布|補足|ハイライト|参考資料|"
                      r"databook|data book|fact ?book|ファクト|haifu|setsumei|briefing", re.I)
NOT_DOC = re.compile(r"短信|tanshin|summary|招集|有価証券報告書|統合報告|株主通信|ガバナンス|定款|中期経営計画", re.I)
# 説明資料そのものではないもの(質疑応答・書き起こし・動画、予実差異・月次・配当などのお知らせ)
NOT_SLIDES = re.compile(r"質疑|書き起こし|書起し|スクリプト|script|動画|音声|Q\s*&\s*A|差異|月次|配当|自己株式|招集", re.I)
TANSHIN_OK = re.compile(r"補足|説明資料|説明会資料|概要|ハイライト|参考資料|プレゼン", re.I)
TDNET_FILE = re.compile(r"/14012\d{13}\.pdf$|/0[89]12\d{14}", re.I)
ENGLISH = re.compile(r"english|英文|英語|_e\.pdf|_en\.pdf|/en/|/english/", re.I)
PERIOD = {
    "1Q": re.compile(r"第\s*[1１一]\s*四半期|1Q|Q1|第１四半期", re.I),
    "2Q": re.compile(r"第\s*[2２二]\s*四半期|2Q|Q2|中間|上期|上半期", re.I),
    "3Q": re.compile(r"第\s*[3３三]\s*四半期|3Q|Q3", re.I),
    "通期": re.compile(r"通期|期末|本決算|年度決算|FY|4Q|Q4|第\s*[4４]\s*四半期|決算説明会", re.I),
}


class IRSite:
    def __init__(self, session: RateLimitedSession, user_agent: str):
        self.s = session
        self.ua = user_agent
        self._robots: dict[str, robotparser.RobotFileParser | None] = {}
        self._pw = self._browser = self._ctx = None
        self._last_render = 0.0

    def allowed(self, url: str) -> bool:
        p = urlparse(url)
        host = f"{p.scheme}://{p.netloc}"
        if host not in self._robots:
            rp = robotparser.RobotFileParser()
            try:
                r = self.s.get(host + "/robots.txt")
                rp.parse(r.text.splitlines() if r is not None and r.ok else [])
            except Exception:
                rp.parse([])
            self._robots[host] = rp
        return self._robots[host].can_fetch(self.ua, url)

    def links(self, url: str, render: bool = False) -> list[tuple[str, str]] | None:
        """ページ内のリンク [(絶対URL, 文言)]。render=True はブラウザで表示してから取る(JavaScriptで作る一覧用)。"""
        if render:
            return self._render_links(url)
        soup = self.page(url)
        return None if soup is None else _links(soup)

    def _render_links(self, url: str) -> list[tuple[str, str]] | None:
        if not self.allowed(url):
            return None
        if self._browser is None:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch()
            self._ctx = self._browser.new_context(user_agent=self.ua, locale="ja-JP")
            self._ctx.set_default_timeout(15000)      # どの操作も15秒で打ち切る(固まり防止)
        wait = self.s.min_interval - (time.monotonic() - self._last_render)
        if wait > 0:
            time.sleep(wait)
        self._last_render = time.monotonic()
        pg = self._ctx.new_page()
        try:
            try:
                pg.goto(url, wait_until="networkidle", timeout=20000)
            except Exception:
                pass                                  # 読み込み途中でも、出ている分のリンクは使う
            out = []
            for fr in pg.frames:                      # iframe(外部IRサービス)の中も見る
                try:
                    out += fr.eval_on_selector_all(
                        "a[href]", "els => els.map(e => [e.href, (e.innerText || e.title || '').trim()])")
                except Exception:
                    continue
            return [(h, t) for h, t in out if h.startswith("http")]
        except Exception as e:
            log.debug("ブラウザ表示失敗 %s: %s", url, e)
            return None
        finally:
            pg.close()

    def close(self) -> None:
        if self._browser is not None:
            self._browser.close()
            self._pw.stop()
            self._browser = None

    def page(self, url: str) -> BeautifulSoup | None:
        if not self.allowed(url):
            return None
        try:
            r = self.s.get(url)
        except Exception as e:
            log.debug("IRページ取得失敗 %s: %s", url, e)
            return None
        if r is None or "html" not in r.headers.get("Content-Type", "html"):
            return None
        r.encoding = r.apparent_encoding if r.encoding in (None, "ISO-8859-1") else r.encoding
        soup = BeautifulSoup(r.text, "html.parser")
        soup.base_url = r.url
        return soup


def _links(soup: BeautifulSoup) -> list[tuple[str, str]]:
    """(絶対URL, リンクの文言)。文言が空なら title / alt / 親要素の文字を使う。"""
    out = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        text = a.get_text(" ", strip=True) or a.get("title", "") or " ".join(
            i.get("alt", "") for i in a.find_all("img"))
        # 文言が無いアイコンだけのリンクは、そのまとまり(行)にリンクが1つだけのときに限り周りの文字を使う
        if len(text) < 4:
            for parent in a.parents:
                if parent.name in ("li", "tr", "dd", "dl", "p", "div") and len(parent.find_all("a")) == 1:
                    text = parent.get_text(" ", strip=True)[:120]
                    break
                if len(parent.find_all("a")) > 1:
                    break
        out.append((urljoin(soup.base_url, href), text))
    for f in soup.find_all("iframe", src=True):
        out.append((urljoin(soup.base_url, f["src"]), "iframe"))
    return out


def _same_site(url: str, home: str) -> bool:
    a, b = urlparse(url).netloc.lower(), urlparse(home).netloc.lower()
    root = ".".join(b.split(".")[-3:]) if b.endswith(".jp") else ".".join(b.split(".")[-2:])
    return a.endswith(root) or bool(EXTERNAL_IR_HOST.search(a))


def _dates_in(url: str) -> list[date]:
    out = []
    for y, m, d in re.findall(r"(20\d{2})[-_/]?(0[1-9]|1[0-2])[-_/]?([0-3]\d)", url):
        try:
            out.append(date(int(y), int(m), int(d)))
        except ValueError:
            pass
    return out


def score(url: str, text: str, ann: date, period: str, fy_label: str) -> int:
    url_txt = unquote(url)                        # ファイル名の日本語(「決算短信」など)も見る
    blob = f"{text} {url_txt}"
    sc = 0
    if DOC_TYPE.search(blob):
        sc += 6
    if (NOT_DOC.search(text) and not DOC_TYPE.search(text)) or             (NOT_DOC.search(urlparse(url_txt).path) and not DOC_TYPE.search(urlparse(url_txt).path)):
        sc -= 10                                  # 短信そのもの等
    if "短信" in blob and not TANSHIN_OK.search(blob.replace("決算短信", "")):
        sc -= 10                                  # 「決算資料: …決算短信」のように短信を指しているもの
    if TDNET_FILE.search(url) and "短信" in text:
        sc -= 10
    if NOT_SLIDES.search(blob):
        sc -= 10
    mentioned = [q for q, pat in PERIOD.items() if q != "通期" and pat.search(blob)]
    if period and mentioned and period not in mentioned:
        sc -= 6                                   # 別の四半期の資料
    if ENGLISH.search(blob):
        sc -= 4
    if period and PERIOD.get(period) and PERIOD[period].search(blob):
        sc += 3
    if fy_label and fy_label.replace(" ", "") in blob.replace(" ", ""):
        sc += 3
    fys = {f"{y}年{int(m)}月期" for y, m in re.findall(r"(20\d{2})年\s*(\d{1,2})月期",
                                                        blob.translate(str.maketrans("０１２３４５６７８９", "0123456789")))}
    if fy_label and fys and fy_label not in fys:
        sc -= 8                                   # 表題に別の決算期が書いてある
    ym = [(int(y), int(m)) for y, m in re.findall(r"/(20\d{2})/(0[1-9]|1[0-2])/", url)]
    if ym and all(abs((y * 12 + m) - (ann.year * 12 + ann.month)) > 2 for y, m in ym):
        sc -= 6                                   # URLの年月が発表時期と離れている
    ds = _dates_in(url)
    if any(-3 <= (d - ann).days <= 14 for d in ds):
        sc += 4
    elif ds and all(abs((d - ann).days) > 60 for d in ds):
        sc -= 6                                   # 明らかに別の時期の資料
    if re.search(rf"{ann.year}[/_-]?{ann.month:02d}", url):
        sc += 1
    return sc


NAV_TIERS = [
    re.compile(r"説明会|説明資料|プレゼン|presentation|IR資料|IRライブラリ|ライブラリ|library|決算資料|決算関連|決算説明|"
               r"決算短信|決算情報|決算発表|materials?|briefing", re.I),
    IR_LINK,
    re.compile(r"業績|財務|financial|results|ニュース|news|新着|お知らせ", re.I),
]


def _norm(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc.lower()}{p.path.rstrip('/') or '/'}" + (f"?{p.query}" if p.query else "")


def _nav_tier(link: str, text: str) -> int | None:
    blob = text + " " + urlparse(link).path
    for i, pat in enumerate(NAV_TIERS):
        if pat.search(blob):
            return i
    return 0 if text == "iframe" else None


def find_presentation(site: IRSite, home: str, ann: date, period: str, fy_label: str,
                      max_pages: int = 10, render: bool = False) -> dict | None:
    """会社サイトから説明資料PDFを探す。見つからなければ None。
    「説明会・IR資料・ライブラリ」→「IR・投資家」→「業績・ニュース」の順に優先度つきでたどる
    (深さ2まで。「説明会・IR資料・ライブラリ」系は深さ3まで)。"""
    if not home.startswith("http"):
        home = "http://" + home
    seen: set[str] = set()
    pdfs: dict[str, str] = {}
    queue: list[tuple[int, int, str]] = [(0, 0, home)]          # (優先度, 深さ, URL)
    fetched = 0
    while queue and fetched < max_pages:
        queue.sort()
        _, depth, url = queue.pop(0)
        key = _norm(url)
        if key in seen:
            continue
        seen.add(key)
        found = site.links(url, render)
        fetched += 1
        if found is None:
            continue
        for link, text in found:
            if link.lower().split("?")[0].endswith(".pdf"):
                if len(text) > len(pdfs.get(link, "")):
                    pdfs[link] = text
                continue
            if depth >= 3 or _norm(link) in seen or not _same_site(link, home):
                continue
            tier = _nav_tier(link, text)
            if tier is not None and depth == 2 and tier != 0:
                continue                          # 3段目は「説明会・IR資料・ライブラリ」系のリンクだけ
            if tier is not None and all(_norm(q[2]) != _norm(link) for q in queue):
                queue.append((tier * 10 + depth, depth + 1, link))

    best = None
    for link, text in pdfs.items():
        sc = score(link, text, ann, period, fy_label)
        if sc >= 9 and (best is None or sc > best["score"]):
            best = {"url": link, "title": text[:120], "score": sc}
    return best


def fy_label_of(title: str) -> str:
    """短信の表題から「2027年2月期」を取り出す(全角数字も半角に)。"""
    t = title.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    m = re.search(r"(20\d{2})年\s*(\d{1,2})月期", t)
    return f"{m.group(1)}年{int(m.group(2))}月期" if m else ""


def window(ann: date, days: int = 14) -> tuple[date, date]:
    return ann - timedelta(days=1), ann + timedelta(days=days)
