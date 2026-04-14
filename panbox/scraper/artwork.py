"""从 TMDB CDN 下载海报/fanart 为 bytes。"""
from __future__ import annotations

from typing import Optional

import requests

IMG_BASE = "https://image.tmdb.org/t/p"


def build_url(path: Optional[str], size: str = "original") -> Optional[str]:
    if not path:
        return None
    return f"{IMG_BASE}/{size}{path}"


def download(url: str, timeout: int = 30) -> bytes:
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    return r.content
