"""JPX公式の公開データ。

- 上場銘柄一覧 data_j.xlsx(月次更新): 市場区分・33業種
- 決算発表予定日 kessanMM_MMDD.xlsx(四半期末の月ごと、随時更新): 各社が取引所に届け出た発表予定日
"""
import io
import re

import pandas as pd

from .http import RateLimitedSession

BASE = "https://www.jpx.co.jp"
LISTED_PAGE = BASE + "/markets/statistics-equities/misc/01.html"
SCHEDULE_PAGE = BASE + "/listing/event-schedules/financial-announcement/index.html"

FISCAL_Q = {"第１四半期": "Q1", "第２四半期": "Q2", "第３四半期": "Q3", "第４四半期": "Q4", "本決算": "FY",
            "第1四半期": "Q1", "第2四半期": "Q2", "第3四半期": "Q3", "第4四半期": "Q4"}


def _links(html: str, pattern: str) -> list[str]:
    hrefs = re.findall(r'href="([^"]+)"', html)
    return [h if h.startswith("http") else BASE + h for h in dict.fromkeys(hrefs) if re.search(pattern, h)]


def fetch_listed(session: RateLimitedSession) -> pd.DataFrame:
    """上場銘柄一覧。列: code, name, market, sector33"""
    page = session.get(LISTED_PAGE)
    url = _links(page.content.decode("utf-8", "replace"), r"data_j\.xlsx?$")[0]
    return parse_listed(session.get(url).content)


def parse_listed(xlsx: bytes) -> pd.DataFrame:
    d = pd.read_excel(io.BytesIO(xlsx), dtype=str)
    d = d.rename(columns={"コード": "code", "銘柄名": "name", "市場・商品区分": "market_raw", "33業種区分": "sector33"})
    d["code"] = d["code"].str.strip()
    d["market"] = d["market_raw"].str.extract(r"^(プライム|スタンダード|グロース)")[0].fillna("")
    return d[["code", "name", "market", "market_raw", "sector33"]]


def fetch_schedule(session: RateLimitedSession) -> pd.DataFrame:
    """決算発表予定日(全ファイル結合)。列: announce_date, code, name, fiscal_q, market"""
    page = session.get(SCHEDULE_PAGE)
    urls = _links(page.content.decode("utf-8", "replace"), r"kessan\d{2}_\d{4}\.xlsx?$")
    frames = [parse_schedule(session.get(u).content) for u in urls]
    if not frames:
        return pd.DataFrame(columns=["announce_date", "code", "name", "fiscal_q", "market"])
    out = pd.concat(frames, ignore_index=True)
    # 同一銘柄が複数ファイルにある場合(決算期変更など)は近い日付を残す
    return out.sort_values("announce_date").drop_duplicates("code", keep="first").reset_index(drop=True)


def parse_schedule(xlsx: bytes) -> pd.DataFrame:
    raw = pd.read_excel(io.BytesIO(xlsx), header=None, dtype=str)
    # 見出し行(「決算発表予定日」を含む行)の次からがデータ
    head = raw.index[raw[0].fillna("").str.contains("決算発表予定日|Scheduled Dates")][0]
    d = raw.iloc[head + 1:, [0, 1, 2, 7, 9]]
    d.columns = ["announce_date", "code", "name", "fiscal_q", "market"]
    d = d[d["code"].fillna("").str.fullmatch(r"[0-9A-Z]{4}")].copy()
    d["announce_date"] = pd.to_datetime(d["announce_date"], errors="coerce").dt.date  # 「未定」は NaT
    d = d.dropna(subset=["announce_date"])
    d["fiscal_q"] = d["fiscal_q"].map(FISCAL_Q).fillna("")
    d["name"] = d["name"].str.strip()
    return d.reset_index(drop=True)
