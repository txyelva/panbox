from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Optional

from .clouds.base import RemoteFile
from .matcher import _cn_to_int


_DATE_YYYYMMDD = re.compile(r"(?<!\d)(20\d{2})[.\-_/年]?\s*(\d{2})[.\-_/月]?\s*(\d{2})(?!\d)")
_DATE_YYMMDD = re.compile(r"(?<!\d)(\d{2})[.\-_/](\d{2})[.\-_/](\d{2})(?!\d)")
_PERIOD_RE = re.compile(
    r"第\s*([0-9零〇一二两三四五六七八九十]+)\s*(?:期|集)\s*[：:：,，、.\-_\s]*([上中下])?"
)
_PAREN_PART_RE = re.compile(r"[（(]\s*([上中下])\s*[）)]")
_GENERIC_EP_RE = re.compile(r"^第\s*\d+\s*集$")

_NEGATIVE_KEYWORDS = (
    "加更",
    "会员",
    "彩蛋",
    "精华",
    "花絮",
    "预告",
    "直播",
    "发布会",
    "跑男来了",
    "训练室",
    "全纪录",
    "全记录",
    "纯享",
    "超前",
    "企划",
    "火锅局",
    "天真时间",
    "天真时刻",
    "姐姐请上车",
    "直击",
    "红毯",
    "倒计时",
    "运动会",
    "特别企划",
    "幕后",
)


@dataclass(frozen=True)
class VarietyEpisode:
    number: int
    name: str
    air_date: Optional[date]
    period: Optional[int]
    part: Optional[str]
    keywords: tuple[str, ...]


@dataclass(frozen=True)
class VarietyMatch:
    file: RemoteFile
    episode: VarietyEpisode
    score: int
    reasons: tuple[str, ...]


def build_variety_episodes(season_details: dict[str, Any]) -> list[VarietyEpisode]:
    episodes: list[VarietyEpisode] = []
    for item in season_details.get("episodes") or []:
        number = item.get("episode_number")
        if number is None:
            continue
        name = str(item.get("name") or "")
        period, part = extract_period_part(name)
        if period is None and _GENERIC_EP_RE.match(name.strip()):
            period = int(number)
        episodes.append(
            VarietyEpisode(
                number=int(number),
                name=name,
                air_date=parse_date(str(item.get("air_date") or "")),
                period=period,
                part=part,
                keywords=episode_keywords(name),
            )
        )
    return episodes


def match_variety_files(
    files: list[RemoteFile],
    episodes: list[VarietyEpisode],
    *,
    min_score: int = 80,
) -> list[VarietyMatch]:
    """Match messy variety-show files to the canonical TMDB season episodes.

    The matcher is intentionally conservative: when both sides have dates, the
    dates must match. Extras such as 加更/会员/彩蛋/纯享 are rejected unless TMDB
    explicitly names the episode that way.
    """
    best_by_episode: dict[int, VarietyMatch] = {}

    for f in files:
        best: Optional[VarietyMatch] = None
        for ep in episodes:
            hit = score_file_for_episode(f, ep)
            if hit is None or hit.score < min_score:
                continue
            if best is None or hit.score > best.score:
                best = hit
        if best is None:
            continue
        prev = best_by_episode.get(best.episode.number)
        if prev is None or best.score > prev.score:
            best_by_episode[best.episode.number] = best

    return [best_by_episode[n] for n in sorted(best_by_episode)]


def score_file_for_episode(
    file: RemoteFile,
    episode: VarietyEpisode,
) -> Optional[VarietyMatch]:
    name = normalize_text(file.name)
    ep_name = normalize_text(episode.name)
    file_date = parse_date(file.name)
    file_period, file_part = extract_period_part(file.name)

    if any(k in name and k not in ep_name for k in _NEGATIVE_KEYWORDS):
        return None

    date_delta: Optional[int] = None
    if file_date and episode.air_date:
        date_delta = abs((file_date - episode.air_date).days)
        if date_delta > 1:
            return None

    if (
        file_period is not None
        and episode.period is not None
        and file_period != episode.period
    ):
        return None

    if file_part and episode.part and file_part != episode.part:
        return None

    if date_delta == 1:
        if file_period is None or episode.period is None or file_period != episode.period:
            return None
        if episode.part and file_part != episode.part:
            return None

    score = 0
    reasons: list[str] = []

    if file_date and episode.air_date and file_date == episode.air_date:
        score += 100
        reasons.append("date")
    elif date_delta == 1:
        score += 70
        reasons.append("date±1")

    if file_period is not None and episode.period is not None:
        if file_period == episode.period:
            score += 35
            reasons.append("period")
            if episode.part:
                if file_part == episode.part:
                    score += 30
                    reasons.append("part")
                elif file_part is None:
                    score -= 15

    keyword_hits = [kw for kw in episode.keywords if kw and kw in name]
    if keyword_hits:
        score += min(30, 10 * len(keyword_hits))
        reasons.extend(f"kw:{kw}" for kw in keyword_hits[:3])

    if score <= 0:
        return None
    return VarietyMatch(file=file, episode=episode, score=score, reasons=tuple(reasons))


def parse_date(text: str) -> Optional[date]:
    m = _DATE_YYYYMMDD.search(text)
    if m:
        return _date_from_parts(m.group(1), m.group(2), m.group(3))
    m = _DATE_YYMMDD.search(text)
    if m:
        year = int(m.group(1))
        full_year = 2000 + year if year < 80 else 1900 + year
        return _date_from_parts(str(full_year), m.group(2), m.group(3))
    return None


def extract_period_part(text: str) -> tuple[Optional[int], Optional[str]]:
    m = _PERIOD_RE.search(text)
    if not m:
        return None, None
    period = _cn_to_int(m.group(1))
    part = m.group(2)
    if not part:
        right = text[m.end() : m.end() + 4]
        pm = _PAREN_PART_RE.search(right)
        if pm:
            part = pm.group(1)
    if not part:
        pm = _PAREN_PART_RE.search(text)
        if pm:
            part = pm.group(1)
    return period, part


def episode_keywords(text: str) -> tuple[str, ...]:
    text = normalize_text(text)
    text = _PERIOD_RE.sub(" ", text)
    text = _PAREN_PART_RE.sub(" ", text)
    for token in ("上", "中", "下"):
        text = re.sub(rf"(?<=\s){token}(?=\s)", " ", text)
    pieces = re.split(r"[\s:：,，、/\\\-_.·（）()【】\[\]xX]+", text)
    out: list[str] = []
    for piece in pieces:
        p = piece.strip()
        if len(p) < 2 or p in {"第", "集", "期"}:
            continue
        out.append(p)
    return tuple(dict.fromkeys(out))


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("（", "(").replace("）", ")")).strip()


def _date_from_parts(year: str, month: str, day: str) -> Optional[date]:
    try:
        return date(int(year), int(month), int(day))
    except ValueError:
        return None
