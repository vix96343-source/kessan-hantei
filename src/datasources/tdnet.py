"""TDnet 適時開示情報閲覧サービス (https://www.release.tdnet.info/) のデータソース。

- 一覧ページ: I_list_{page:03d}_{YYYYMMDD}.html (1ページ100件、約1ヶ月分のみ公開)
- 業績予想修正のXBRL (tse-rvfc-*-ixbrl.htm) から修正率を読む
"""
import io
import json
import logging
import math
import re
import zipfile
from dataclasses import asdict, dataclass
from datetime import date

from bs4 import BeautifulSoup

from .http import RateLimitedSession

log = logging.getLogger(__name__)

BASE_URL = "https://www.release.tdnet.info/inbs/"
PAGE_SIZE = 100


@dataclass
class Disclosure:
    disclosure_id: str
    disclosed_at: str      # JST "YYYY-MM-DDTHH:MM"
    code: str              # 4桁(英字コード含む)
    name: str
    title: str
    pdf_url: str
    xbrl_url: str
    exchange: str

    def to_dict(self) -> dict:
        return asdict(self)


def list_url(d: date, page: int) -> str:
    return f"{BASE_URL}I_list_{page:03d}_{d:%Y%m%d}.html"


def normalize_code(raw: str) -> str:
    """TDnetの5桁コード(末尾チェック桁0)を4桁に。"""
    raw = raw.strip()
    if len(raw) == 5 and raw.endswith("0"):
        return raw[:4]
    return raw


def parse_list_page(html: str, d: date) -> tuple[list[Disclosure], int]:
    """一覧ページをパースして (開示リスト, 当日の総件数) を返す。"""
    soup = BeautifulSoup(html, "html.parser")
    total = 0
    summ = soup.select_one(".kaijiSum")
    if summ:
        m = re.search(r"全\s*(\d+)\s*件", summ.get_text())
        if m:
            total = int(m.group(1))

    out = []
    for tr in soup.select("#main-list-table tr"):
        def cell(cls):
            return tr.select_one(f"td.{cls}")
        t_time, t_code, t_name, t_title = cell("kjTime"), cell("kjCode"), cell("kjName"), cell("kjTitle")
        if not (t_time and t_code and t_title):
            continue
        a = t_title.find("a")
        if not a or not a.get("href"):
            continue
        pdf = a["href"]
        xbrl_a = cell("kjXbrl").find("a") if cell("kjXbrl") else None
        out.append(Disclosure(
            disclosure_id=re.sub(r"\.pdf$", "", pdf),
            disclosed_at=f"{d:%Y-%m-%d}T{t_time.get_text(strip=True)}",
            code=normalize_code(t_code.get_text()),
            name=t_name.get_text(strip=True) if t_name else "",
            title=a.get_text(strip=True),
            pdf_url=BASE_URL + pdf,
            xbrl_url=BASE_URL + xbrl_a["href"] if xbrl_a and xbrl_a.get("href") else "",
            exchange=cell("kjPlace").get_text(strip=True) if cell("kjPlace") else "",
        ))
    return out, total


def fetch_day(session: RateLimitedSession, d: date) -> list[Disclosure] | None:
    """指定日の全開示を取得。一覧が存在しない(公開期間外/未来日)ならNone。"""
    r = session.get(list_url(d, 1))
    if r is None:
        return None
    r.encoding = "utf-8"
    items, total = parse_list_page(r.text, d)
    for page in range(2, math.ceil(total / PAGE_SIZE) + 1):
        r = session.get(list_url(d, page))
        if r is None:
            break
        r.encoding = "utf-8"
        items.extend(parse_list_page(r.text, d)[0])
    return items


# ---------------------------------------------------------------- 表題の分類

_CORRECTION = re.compile(r"^[\s(（【]*訂正|一部訂正|訂正版")
_FORECAST = re.compile(r"業績予想|業績見通し|業績予測")
_DIVIDEND = re.compile(r"配当予想|配当の予想|増配|減配|無配|記念配当|特別配当")
_REVISION = re.compile(r"修正|上方|下方|増配|減配|無配")
_DIFF = re.compile(r"差異")
_EARNINGS = re.compile(r"決算短信")
_PRESENTATION = re.compile(r"決算説明|説明会資料|補足説明|補足資料|決算短信補足|決算補足")


def classify_title(title: str) -> dict:
    """表題から開示種別・訂正フラグ・方向(表題ベース)を判定する。"""
    t = title.strip()
    is_correction = bool(_CORRECTION.search(t))

    if _PRESENTATION.search(t):
        kind = "earnings_presentation"
    elif _EARNINGS.search(t):
        kind = "earnings_report"
    else:
        fc, dv, rev = bool(_FORECAST.search(t)), bool(_DIVIDEND.search(t)), bool(_REVISION.search(t))
        if fc and dv and rev:
            kind = "forecast_dividend_revision"
        elif fc and rev:
            kind = "forecast_revision"
        elif dv and rev:
            kind = "dividend_revision"
        elif _DIFF.search(t):
            kind = "actual_vs_forecast"
        elif fc:
            kind = "forecast_initial"   # 未定だった予想の初公表など
        else:
            kind = "other"

    if "上方修正" in t or "増配" in t:
        direction = "up"
    elif "下方修正" in t or "減配" in t or "無配" in t:
        direction = "down"
    else:
        direction = ""
    if "上方修正" in t and "下方修正" in t:
        direction = "mixed"
    return {"kind": kind, "is_correction": is_correction, "title_direction": direction}


# ---------------------------------------------------------------- 業績予想修正XBRL

# 空要素 <ix:nonFraction .../> (未定・該当なし) も1要素として消費しないと値がずれる
_IX_TAG = re.compile(r"<ix:nonFraction\b([^>]*?)(?:/>|>(.*?)</ix:nonFraction>)", re.S)
_ATTR = re.compile(r'(\w+)="([^"]*)"')

# 修正率を採用する指標の優先順位(先にマッチしたものを代表値にする)
_METRIC_PRIORITY = [
    re.compile(r"Operating(Income|Profit)"),
    re.compile(r"Ordinary(Income|Profit)"),
    re.compile(r"ProfitAttributable|NetIncome|Profit"),
    re.compile(r"Sales|Revenue"),
]


def _to_number(text: str, attrs: dict) -> float | None:
    s = re.sub(r"<[^>]+>", "", text).replace(",", "").strip()
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if attrs.get("sign") == "-" else v


def _extract_facts(ixbrl: str) -> dict[tuple[str, str], float]:
    facts = {}
    for m in _IX_TAG.finditer(ixbrl):
        attrs = dict(_ATTR.findall(m.group(1)))
        name, ctx = attrs.get("name", ""), attrs.get("contextRef", "")
        v = _to_number(m.group(2) or "", attrs)
        if v is not None and name and ctx:
            facts[(name.split(":")[-1], ctx)] = v
    return facts


def _pick_period(facts) -> str | None:
    """通期(CurrentYear) > 次期(NextYear) > 中間(Q2) の順で修正後予想を持つ期間を選ぶ。"""
    periods = {ctx.split("_")[0] for (_, ctx) in facts if "CurrentMember" in ctx}
    for p in ("CurrentYearDuration", "NextYearDuration", "CurrentAccumulatedQ2Duration"):
        if p in periods:
            return p
    return next(iter(sorted(periods)), None)


def _value(facts, name, ctx_prefix, ctx_suffix):
    """ForecastMember の値、無ければ Upper/Lower の中点。"""
    def find(member):
        for (n, ctx), v in facts.items():
            if n == name and ctx.startswith(ctx_prefix) and ctx.endswith(f"{ctx_suffix}_{member}"):
                return v
        return None
    v = find("ForecastMember")
    if v is not None:
        return v
    up, lo = find("UpperMember"), find("LowerMember")
    if up is not None and lo is not None:
        return (up + lo) / 2
    return None


def parse_forecast_revision_xbrl(zip_bytes: bytes) -> dict:
    """業績予想修正XBRL(zip)から代表指標の修正率を返す。読めなければ空dict。"""
    try:
        z = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile:
        return {}
    htm = [n for n in z.namelist() if n.endswith("-ixbrl.htm") and "rvfc" in n]
    if not htm:
        return {}
    facts = _extract_facts(z.read(htm[0]).decode("utf-8", errors="replace"))
    period = _pick_period(facts)
    if not period:
        return {}
    scope = "ConsolidatedMember" if any("_ConsolidatedMember_" in c for (_, c) in facts) else "NonConsolidatedMember"
    prefix = f"{period}_{scope}"

    base_names = sorted({n[len("ChangeIn"):] for (n, c) in facts if n.startswith("ChangeIn") and c.startswith(prefix)}
                        | {n for (n, c) in facts if c.startswith(prefix) and "PreviousMember" in c
                           and not n.startswith(("ChangeIn", "AmountChange")) and "PerShare" not in n})
    changes = {}
    for base in base_names:
        pct = _value(facts, f"ChangeIn{base}", prefix, "CurrentMember")
        if pct is None:
            prev = _value(facts, base, prefix, "PreviousMember")
            cur = _value(facts, base, prefix, "CurrentMember")
            if prev not in (None, 0) and cur is not None:
                pct = round((cur - prev) / abs(prev) * 100, 1)
        if pct is not None:
            changes[base] = pct

    metric = None
    for pat in _METRIC_PRIORITY:
        metric = next((b for b in base_names if b in changes and pat.search(b)), None)
        if metric:
            break
    if metric is None:
        return {"period": period, "changes": changes}
    pct = changes[metric]
    return {
        "period": period,
        "metric": metric,
        "change_pct": pct,
        "direction": "up" if pct > 0 else "down" if pct < 0 else "flat",
        "changes": changes,
    }


def fetch_forecast_revision(session: RateLimitedSession, xbrl_url: str) -> dict:
    r = session.get(xbrl_url)
    if r is None:
        return {}
    return parse_forecast_revision_xbrl(r.content)


def changes_json(changes: dict) -> str:
    return json.dumps(changes, ensure_ascii=False, sort_keys=True) if changes else ""
