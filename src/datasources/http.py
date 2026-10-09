"""レート制限・リトライ付きHTTPクライアント(全データソース共通)。"""
import logging
import time

import requests

log = logging.getLogger(__name__)


class RateLimitedSession:
    def __init__(self, user_agent: str, min_interval_sec: float = 1.0,
                 retries: int = 3, timeout_sec: float = 20):
        self._s = requests.Session()
        self._s.headers["User-Agent"] = user_agent
        self.min_interval = min_interval_sec
        self.retries = retries
        self.timeout = timeout_sec
        self._last = 0.0

    @classmethod
    def from_config(cls, cfg: dict) -> "RateLimitedSession":
        h = cfg["http"]
        return cls(h["user_agent"], h["min_interval_sec"], h["retries"], h["timeout_sec"])

    def get(self, url: str) -> requests.Response | None:
        """GETする。404はNoneを返す。それ以外の失敗はリトライ後に例外。"""
        for attempt in range(1, self.retries + 1):
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                r = self._s.get(url, timeout=self.timeout)
                if r.status_code == 404:
                    return None
                r.raise_for_status()
                return r
            except requests.RequestException as e:
                if attempt == self.retries:
                    raise
                log.warning("GET失敗 (%d/%d) %s: %s", attempt, self.retries, url, e)
                time.sleep(2 ** attempt)
        return None
