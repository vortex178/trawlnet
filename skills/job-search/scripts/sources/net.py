"""Shared HTTP and config helpers for the fetchers. Boards call these as `net.get(...)` so tests patch them in one place."""
from __future__ import annotations

import json
import re
import urllib.request
from functools import lru_cache

from common import UA, load_config


@lru_cache(maxsize=1)
def cfg() -> dict:
    return load_config()


def pack() -> dict:
    return cfg()["pack"]


def places_rx() -> str:
    return "|".join(re.escape(p) for p in pack()["country_places"])


def get(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def get_json(url: str):
    return json.loads(get(url))


def post_json(url: str, body: dict, headers: dict | None = None):
    h = {"User-Agent": UA, "Content-Type": "application/json", "Accept": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=h)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())
