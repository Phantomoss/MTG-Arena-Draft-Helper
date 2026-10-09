"""A tiny polite HTTP client: identifies itself, spaces requests out, retries on 429/5xx."""

from __future__ import annotations

import time

import requests

from . import config


class PoliteSession:
    def __init__(self, delay_s: float):
        self.delay_s = delay_s
        self._last = 0.0
        self._session = requests.Session()
        self._session.headers.update(
            {"User-Agent": config.USER_AGENT, "Accept": "application/json"}
        )

    def get_json(self, url: str, params: dict | None = None, allow_404: bool = False):
        """GET a URL and return parsed JSON. Returns None on 404 if allow_404 is set."""
        for attempt in range(4):
            wait = self.delay_s - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            resp = self._session.get(url, params=params, timeout=config.REQUEST_TIMEOUT_S)
            self._last = time.monotonic()
            if resp.status_code == 404 and allow_404:
                return None
            if resp.status_code == 429 or resp.status_code >= 500:
                time.sleep(2**attempt)  # back off: 1s, 2s, 4s, 8s
                continue
            resp.raise_for_status()
            return resp.json()
        resp.raise_for_status()
        raise RuntimeError(f"Giving up on {url}")
