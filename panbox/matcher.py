from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional, Union

from guessit import guessit

_CN_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}


def _cn_to_int(s: str) -> Optional[int]:
    if s.isdigit():
        return int(s)
    if len(s) == 1 and s in _CN_DIGITS:
        return _CN_DIGITS[s]
    if len(s) == 2 and s.startswith("十"):
        return 10 + _CN_DIGITS.get(s[1], 0)
    if len(s) == 2 and s.endswith("十"):
        return _CN_DIGITS.get(s[0], 0) * 10
    if len(s) == 3 and s[1] == "十":
        return _CN_DIGITS.get(s[0], 0) * 10 + _CN_DIGITS.get(s[2], 0)
    return None


_SEASON_PATTERNS = [
    re.compile(r"第\s*([0-9零〇一二两三四五六七八九十]+)\s*季"),
    re.compile(r"[Ss]eason\s*(\d+)"),
    re.compile(r"[Ss](\d{1,2})(?!\d)"),  # 含 S01E01 里的 S01
]


def normalize_query(text: str) -> tuple[str, Optional[int]]:
    """剥离中文/英文季度标记,返回 (纯标题, 季号)"""
    season: Optional[int] = None
    out = text
    for pat in _SEASON_PATTERNS:
        m = pat.search(out)
        if m:
            n = _cn_to_int(m.group(1))
            if n is not None:
                season = n
            out = pat.sub("", out)
            break
    return out.strip(), season


@dataclass
class HintParse:
    title: str
    year: Optional[int] = None
    season: Optional[int] = None


_YEAR_PAREN = re.compile(r"[（(](\d{4})[)）]")
_YEAR_BARE = re.compile(r"(?:^|[^\d])((?:19|20)\d{2})(?:[^\d]|$)")
_SE_RANGE = re.compile(
    r"[Ss](\d{1,2})\s*[Ee]\d{1,3}(?:\s*[-~～]\s*[Ee]?\d{1,3})?"
)
_E_ONLY = re.compile(r"[Ee]\d{1,3}(?:\s*[-~～]\s*[Ee]?\d{1,3})?")
_QUALITY = re.compile(
    r"(?i)\b(4K|2160p|1080p|720p|480p|HDR10?\+?|DolbyVision|DV|10bit|"
    r"WEB[-\s]?DL|WEB[-\s]?Rip|WEBRip|BluRay|Blu-?Ray|BDRip|BDMV|REMUX|"
    r"HDTV|DVDRip|x26[45]|H\.?26[45]|HEVC|AVC|AAC|DTS(?:-HD)?|AC3|"
    r"TrueHD|Atmos|FLAC|MA|HiveWeb|FRDS|CHDBits|CMCT|OurBits|TLF|"
    r"NTG|beAst|HDS|MySiLU|MNHD|PTer|CatEDU|DIY|国语|粤语|中字|"
    r"内封|内嵌|简繁|繁简|中英|双语|高码|压制)\b"
)
_PUNCT_TRIM = " -_.|·•—–《》【】[]()（）"


def parse_hint(text: str) -> HintParse:
    """从用户给的 hint 里抽 title/year/season,剥掉季集范围和画质 tag。"""
    t = text
    year: Optional[int] = None
    season: Optional[int] = None

    m = _YEAR_PAREN.search(t)
    if m:
        year = int(m.group(1))
        t = t.replace(m.group(0), " ")
    else:
        m = _YEAR_BARE.search(t)
        if m:
            year = int(m.group(1))
            t = t[: m.start(1)] + " " + t[m.end(1) :]

    # S01E01-E19 形式:同时抓季号并移除整段
    m = _SE_RANGE.search(t)
    if m:
        season = int(m.group(1))
        t = t[: m.start()] + " " + t[m.end() :]
    else:
        for pat in _SEASON_PATTERNS:
            m = pat.search(t)
            if m:
                n = _cn_to_int(m.group(1))
                if n is not None:
                    season = n
                t = pat.sub(" ", t)
                break

    # 剩下的独立 E01-E19 / E05 也清掉
    t = _E_ONLY.sub(" ", t)
    t = _QUALITY.sub(" ", t)
    t = re.sub(r"[.\-_]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip(_PUNCT_TRIM)

    return HintParse(title=t, year=year, season=season)


@dataclass
class Guess:
    title: str
    year: Optional[int]
    media_type: Optional[str]
    season: Optional[int]
    episode: Union[int, list, None]
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_text(cls, text: str) -> "Guess":
        info: dict[str, Any] = dict(guessit(text))
        gtype = info.get("type")
        if gtype == "episode":
            media_type = "tv"
        elif gtype == "movie":
            media_type = "movie"
        else:
            media_type = None
        return cls(
            title=str(info.get("title") or ""),
            year=info.get("year"),
            media_type=media_type,
            season=info.get("season"),
            episode=info.get("episode"),
            raw={k: _to_jsonable(v) for k, v in info.items()},
        )


def _to_jsonable(v: Any) -> Any:
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    if isinstance(v, (list, tuple)):
        return [_to_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {k: _to_jsonable(x) for k, x in v.items()}
    return str(v)
