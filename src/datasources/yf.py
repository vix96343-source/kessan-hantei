"""yfinance(日本株は「コード.T」)。"""
import logging
import time

import pandas as pd
import yfinance as yf

log = logging.getLogger(__name__)


def _download_chunk(codes: list[str], days: int) -> dict[str, float]:
    tickers = [f"{c}.T" for c in codes]
    try:
        px = yf.download(tickers, period="3mo", interval="1d", group_by="ticker",
                         auto_adjust=False, threads=True, progress=False)
    except Exception as e:
        log.warning("yfinance取得失敗 (%d銘柄): %s", len(codes), e)
        return {}
    out = {}
    for c, t in zip(codes, tickers):
        try:
            df = px[t] if isinstance(px.columns, pd.MultiIndex) else px
            df = df.dropna(subset=["Close", "Volume"])
            df = df[df["Volume"] > 0].tail(days)
        except KeyError:
            continue
        if len(df) >= days // 2:
            out[c] = float((df["Close"] * df["Volume"]).mean())
    return out


def avg_turnover(codes: list[str], days: int = 20, chunk: int = 100,
                 pause_sec: float = 2.0, retry_wait_sec: float = 60.0) -> dict[str, float]:
    """直近days営業日の平均売買代金(円)。終値×出来高で近似。取れない銘柄は含めない。

    Yahooのレート制限に当たりにくいよう小分け+間隔を空け、取れなかった銘柄は1回だけ待って再試行する。
    """
    out = {}
    for i in range(0, len(codes), chunk):
        out.update(_download_chunk(codes[i:i + chunk], days))
        log.info("売買代金 %d/%d (取得 %d)", min(i + chunk, len(codes)), len(codes), len(out))
        time.sleep(pause_sec)

    missing = [c for c in codes if c not in out]
    if missing and len(missing) < len(codes):
        log.info("未取得 %d 銘柄を %d 秒後に再試行", len(missing), retry_wait_sec)
        time.sleep(retry_wait_sec)
        for i in range(0, len(missing), chunk):
            out.update(_download_chunk(missing[i:i + chunk], days))
            time.sleep(pause_sec)
    return out
