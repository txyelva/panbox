from __future__ import annotations

import re
from typing import Optional

from guessit import guessit

_SAFE_CHARS = re.compile(r'[\\/:*?"<>|]')


def _sanitize_tag(name: str) -> str:
    return _SAFE_CHARS.sub("", name).strip()


def _strip_ext(name: str) -> str:
    if "." not in name:
        return name
    return name.rsplit(".", 1)[0]


def _add_tag(tags: list[str], seen: set[str], tag: str | None) -> None:
    if not tag:
        return
    cleaned = _sanitize_tag(str(tag)).strip(" ._-")
    if not cleaned:
        return
    key = re.sub(r"[^a-z0-9+]+", "", cleaned.lower())
    if key in seen:
        return
    seen.add(key)
    tags.append(cleaned)


def _first_match(text: str, patterns: list[tuple[str, str]]) -> Optional[str]:
    for pattern, value in patterns:
        if re.search(pattern, text, re.I):
            return value
    return None


def _raw_resolution(name: str) -> Optional[str]:
    return _first_match(
        name,
        [
            (r"(?<!\d)4320p(?!\d)|\b8K\b", "4320p"),
            (r"(?<!\d)2160p(?!\d)|\b4K\b", "2160p"),
            (r"(?<!\d)1080p(?!\d)", "1080p"),
            (r"(?<!\d)720p(?!\d)", "720p"),
            (r"(?<!\d)480p(?!\d)", "480p"),
        ],
    )


def _raw_source(name: str) -> Optional[str]:
    return _first_match(
        name,
        [
            (r"\bWEB[\s._-]?DL\b", "WEB-DL"),
            (r"\bWEB[\s._-]?Rip\b|\bWEBRip\b", "WEBRip"),
            (r"\bBlu[\s._-]?Ray\b", "BluRay"),
            (r"\bBDRip\b", "BDRip"),
            (r"\bBDMV\b", "BDMV"),
            (r"\bREMUX\b", "REMUX"),
            (r"\bHDTV\b", "HDTV"),
            (r"\bDVDRip\b", "DVDRip"),
        ],
    )


def _raw_video_codec(name: str) -> Optional[str]:
    return _first_match(
        name,
        [
            (r"\b(?:H[\s._-]?265|HEVC|x265)\b", "H.265"),
            (r"\b(?:H[\s._-]?264|AVC|x264)\b", "H.264"),
            (r"\bAV1\b", "AV1"),
        ],
    )


def _dynamic_range_tags(name: str) -> list[str]:
    tags: list[str] = []
    seen: set[str] = set()
    if re.search(r"\b(?:DoVi|Dolby[\s._-]?Vision|DV)\b", name, re.I):
        _add_tag(tags, seen, "DV")
    if re.search(r"\bHDR10[\s._-]?\+(?!\w)", name, re.I):
        _add_tag(tags, seen, "HDR10+")
    elif re.search(r"\bHDR10\b", name, re.I):
        _add_tag(tags, seen, "HDR10")
    elif re.search(r"\bHDR\b", name, re.I):
        _add_tag(tags, seen, "HDR")
    if re.search(r"\bHLG\b", name, re.I):
        _add_tag(tags, seen, "HLG")
    if re.search(r"\bSDR\b", name, re.I):
        _add_tag(tags, seen, "SDR")
    return tags


def _audio_tags(name: str, guess: dict) -> list[str]:
    tags: list[str] = []
    seen: set[str] = set()
    compact = name.replace("_", ".").replace("-", ".")

    ddp = re.search(r"\b(?:DDP|EAC3|E-AC-3)[\s._-]*(\d(?:[.\s_-]\d)?)?\b", compact, re.I)
    ac3 = re.search(r"\bAC3[\s._-]*(\d(?:[.\s_-]\d)?)?\b", compact, re.I)
    aac = re.search(r"\bAAC[\s._-]*(\d(?:[.\s_-]\d)?)?\b", compact, re.I)
    truehd = re.search(r"\bTrueHD[\s._-]*(\d(?:[.\s_-]\d)?)?\b", compact, re.I)
    dts_ma = re.search(r"\bDTS[\s._-]?HD[\s._-]?MA\b", compact, re.I)
    dts = re.search(r"\bDTS\b", compact, re.I)
    flac = re.search(r"\bFLAC\b", compact, re.I)

    def _with_channels(prefix: str, match: re.Match[str]) -> str:
        channels = match.group(1)
        if not channels:
            return prefix
        return prefix + channels.replace(" ", ".").replace("_", ".").replace("-", ".")

    if ddp:
        _add_tag(tags, seen, _with_channels("DDP", ddp))
    elif truehd:
        _add_tag(tags, seen, _with_channels("TrueHD", truehd))
    elif dts_ma:
        _add_tag(tags, seen, "DTS-HD MA")
    elif dts:
        _add_tag(tags, seen, "DTS")
    elif ac3:
        _add_tag(tags, seen, _with_channels("AC3", ac3))
    elif aac:
        _add_tag(tags, seen, _with_channels("AAC", aac))
    elif flac:
        _add_tag(tags, seen, "FLAC")

    codecs = guess.get("audio_codec")
    if not isinstance(codecs, list):
        codecs = [codecs] if codecs else []
    channels = str(guess.get("audio_channels") or "")
    for codec in codecs:
        label = str(codec)
        mapped = None
        if "Dolby Digital Plus" in label:
            mapped = "DDP" + channels
        elif label == "Dolby Digital":
            mapped = "AC3" + channels
        elif "Dolby Atmos" in label:
            mapped = "Atmos"
        elif label == "Advanced Audio Codec":
            mapped = "AAC" + channels
        elif "TrueHD" in label:
            mapped = "TrueHD" + channels
        elif "DTS-HD" in label:
            mapped = "DTS-HD"
        elif label == "DTS":
            mapped = "DTS"
        elif label == "FLAC":
            mapped = "FLAC"
        _add_tag(tags, seen, mapped)

    if re.search(r"\bAtmos\b", name, re.I):
        _add_tag(tags, seen, "Atmos")
    return tags


def extract_media_tags(source_name: str | None) -> list[str]:
    """Extract high-confidence technical tags worth preserving in library names."""
    if not source_name:
        return []
    name = _strip_ext(source_name)
    try:
        guessed = dict(guessit(source_name))
    except Exception:
        guessed = {}

    tags: list[str] = []
    seen: set[str] = set()

    _add_tag(tags, seen, _raw_resolution(name) or guessed.get("screen_size"))
    _add_tag(tags, seen, _raw_source(name))

    for tag in _audio_tags(name, guessed):
        _add_tag(tags, seen, tag)
    for tag in _dynamic_range_tags(name):
        _add_tag(tags, seen, tag)

    _add_tag(tags, seen, _raw_video_codec(name) or guessed.get("video_codec"))

    bit_depth = _first_match(
        name,
        [
            (r"\b12[\s._-]?bit\b", "12bit"),
            (r"\b10[\s._-]?bit\b", "10bit"),
            (r"\b8[\s._-]?bit\b", "8bit"),
        ],
    )
    _add_tag(tags, seen, bit_depth)

    return tags


def media_suffix(source_name: str | None) -> str:
    tags = extract_media_tags(source_name)
    return (" - " + " ".join(tags)) if tags else ""
