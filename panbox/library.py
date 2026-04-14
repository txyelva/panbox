from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional

from .clouds.base import RemoteFile
from .matcher import Guess

_SAFE_CHARS = re.compile(r'[\\/:*?"<>|]')

# 宽松 SxxExx 匹配:S01E02 / s01.e02 / S01 E02 / S01-E02 / s01_e02
_SXEX_RE = re.compile(r"[sS](\d{1,2})[\s._\-]*[eE](\d{1,3})")
# 备用:1x02 / 01x002
_NXN_RE = re.compile(r"(?<!\d)(\d{1,2})x(\d{1,3})(?!\d)")

# 父目录 → season 推断
_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_SEASON_FOLDER_RES = [
    re.compile(r"[sS](\d{1,2})(?!\d)"),                     # S01 / s1
    re.compile(r"[Ss]eason[\s._\-]*(\d{1,2})"),             # Season 1 / Season_01
    re.compile(r"第\s*(\d{1,2})\s*季"),                      # 第1季 / 第 01 季
    re.compile(r"第\s*([一二三四五六七八九十])\s*季"),           # 第一季
]


def sanitize(name: str) -> str:
    return _SAFE_CHARS.sub("", name).strip()


def extract_sxex(name: str) -> Optional[tuple[int, int]]:
    """从任意命名里抽 (season, episode)。认 S01E02/s01.e02/1x02 等变体。"""
    m = _SXEX_RE.search(name)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = _NXN_RE.search(name)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None


def parse_season_from_name(name: str) -> Optional[int]:
    """从目录名推断 season 号。识别 S01/Season 1/第1季/第一季。"""
    for rx in _SEASON_FOLDER_RES:
        m = rx.search(name)
        if not m:
            continue
        v = m.group(1)
        if v in _CN_NUM:
            return _CN_NUM[v]
        try:
            return int(v)
        except ValueError:
            continue
    return None


def find_season_folder(children: Iterable[RemoteFile], season: int) -> Optional[RemoteFile]:
    """在剧目录子项里找匹配 season 的已存在目录(任意命名格式)。"""
    for c in children:
        if not c.is_dir:
            continue
        if parse_season_from_name(c.name) == season:
            return c
    return None


@dataclass
class Layout:
    """基于 TMDB 元数据算目标路径。"""
    title: str
    year: Optional[str]
    media_type: str      # movie | tv

    @property
    def folder_name(self) -> str:
        base = sanitize(self.title)
        return f"{base} ({self.year})" if self.year else base

    def movie_dir(self, library_movies: str) -> str:
        return f"{library_movies.rstrip('/')}/{self.folder_name}"

    def movie_filename(self, ext: str, part: Optional[int] = None) -> str:
        name = self.folder_name
        if part:
            name = f"{name} - part{part}"
        return f"{name}.{ext}"

    def tv_show_dir(self, library_tv: str) -> str:
        return f"{library_tv.rstrip('/')}/{self.folder_name}"

    def season_dir(self, library_tv: str, season: int) -> str:
        return f"{self.tv_show_dir(library_tv)}/Season {season:02d}"

    def tv_filename(self, season: int, episode: int | list, ext: str) -> str:
        title = sanitize(self.title)
        if isinstance(episode, list) and len(episode) > 1:
            tag = f"S{season:02d}E{int(episode[0]):02d}-E{int(episode[-1]):02d}"
        else:
            ep = episode[0] if isinstance(episode, list) else episode
            tag = f"S{season:02d}E{int(ep):02d}"
        return f"{title} - {tag}.{ext}"


def scan_existing_episodes(files: list[RemoteFile], season: int) -> set[int]:
    """从一个目录的文件列表里提取已有集号(只匹配指定 season)。

    对每个视频,先用宽松 SxxExx 正则抽一次(认 S01E02/s01.e02/1x02 变体),
    抽不到再用 guessit 兜底。guessit 遇到的纯数字文件名会被当作 episode
    (season 可能为 None),此时视该目录的 season 为当前 season,收进集号。
    """
    existing: set[int] = set()
    for f in files:
        if f.is_dir or not f.is_video:
            continue
        hit = extract_sxex(f.name)
        if hit is not None:
            s, ep = hit
            if s == season:
                existing.add(ep)
            continue
        g = Guess.from_text(f.name)
        if g.season is not None and g.season != season:
            continue
        ep = g.episode
        if isinstance(ep, int):
            existing.add(ep)
        elif isinstance(ep, list):
            for e in ep:
                try:
                    existing.add(int(e))
                except (TypeError, ValueError):
                    pass
    return existing
