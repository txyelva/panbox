"""生成 Plex / Emby / Kodi 通用格式的 NFO。

Kodi wiki 是权威参考,Plex 和 Emby 对其 NFO 支持有重叠子集。
我们输出的字段遵循 Kodi movie.nfo / tvshow.nfo / episodedetails.nfo 规范。
"""
from __future__ import annotations

from typing import Any, Optional
from xml.etree import ElementTree as ET

IMG_BASE = "https://image.tmdb.org/t/p/original"


def _el(parent: ET.Element, tag: str, text: Any = None, **attrs: Any) -> ET.Element:
    e = ET.SubElement(parent, tag, {k: str(v) for k, v in attrs.items() if v is not None})
    if text is not None and text != "":
        e.text = str(text)
    return e


def _pretty(root: ET.Element) -> str:
    # Python 3.9+ 有 indent,用它避免手写缩进
    ET.indent(root, space="  ", level=0)
    xml = ET.tostring(root, encoding="unicode")
    return '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + xml


def _extract_year(date_str: Optional[str]) -> Optional[str]:
    if not date_str:
        return None
    return date_str.split("-")[0] if "-" in date_str else date_str


def _add_ids(parent: ET.Element, tmdb_id: int, external_ids: dict) -> None:
    _el(parent, "uniqueid", tmdb_id, type="tmdb", default="true")
    imdb = (external_ids or {}).get("imdb_id")
    if imdb:
        _el(parent, "uniqueid", imdb, type="imdb")
    tvdb = (external_ids or {}).get("tvdb_id")
    if tvdb:
        _el(parent, "uniqueid", tvdb, type="tvdb")


def _add_credits(parent: ET.Element, credits: dict) -> None:
    crew = (credits or {}).get("crew") or []
    cast = (credits or {}).get("cast") or []

    for c in crew:
        if c.get("job") == "Director":
            _el(parent, "director", c.get("name"))
    # Writers
    writers = [c for c in crew if c.get("department") == "Writing"]
    for w in writers[:5]:
        _el(parent, "credits", w.get("name"))

    for a in cast[:15]:
        actor = ET.SubElement(parent, "actor")
        _el(actor, "name", a.get("name"))
        _el(actor, "role", a.get("character"))
        order = a.get("order")
        if order is not None:
            _el(actor, "order", order)
        if a.get("profile_path"):
            _el(actor, "thumb", f"{IMG_BASE}{a['profile_path']}")


def movie_nfo(details: dict) -> str:
    """生成 movie.nfo"""
    root = ET.Element("movie")
    title = details.get("title") or details.get("original_title") or ""
    _el(root, "title", title)
    _el(root, "originaltitle", details.get("original_title"))
    _el(root, "sorttitle", title)
    _el(root, "year", _extract_year(details.get("release_date")))
    _el(root, "premiered", details.get("release_date"))

    _el(root, "plot", details.get("overview"))
    _el(root, "outline", details.get("overview"))
    _el(root, "tagline", details.get("tagline"))

    runtime = details.get("runtime")
    if runtime:
        _el(root, "runtime", runtime)

    if details.get("vote_average") is not None:
        ratings = ET.SubElement(root, "ratings")
        rating = ET.SubElement(
            ratings,
            "rating",
            name="tmdb",
            max="10",
            default="true",
        )
        _el(rating, "value", round(details["vote_average"], 1))
        _el(rating, "votes", details.get("vote_count"))
        _el(root, "rating", round(details["vote_average"], 1))

    for g in details.get("genres") or []:
        _el(root, "genre", g.get("name"))
    for s in details.get("production_companies") or []:
        _el(root, "studio", s.get("name"))
    for c in details.get("production_countries") or []:
        _el(root, "country", c.get("name"))

    if details.get("poster_path"):
        _el(root, "thumb", f"{IMG_BASE}{details['poster_path']}", aspect="poster")
    if details.get("backdrop_path"):
        fanart = ET.SubElement(root, "fanart")
        _el(fanart, "thumb", f"{IMG_BASE}{details['backdrop_path']}")

    _add_ids(root, details["id"], details.get("external_ids") or {})
    _add_credits(root, details.get("credits") or {})

    return _pretty(root)


def tvshow_nfo(details: dict) -> str:
    """生成 tvshow.nfo(放在剧集根目录)"""
    root = ET.Element("tvshow")
    title = details.get("name") or details.get("original_name") or ""
    _el(root, "title", title)
    _el(root, "originaltitle", details.get("original_name"))
    _el(root, "showtitle", title)
    _el(root, "sorttitle", title)
    _el(root, "year", _extract_year(details.get("first_air_date")))
    _el(root, "premiered", details.get("first_air_date"))

    _el(root, "plot", details.get("overview"))
    _el(root, "outline", details.get("overview"))
    _el(root, "status", details.get("status"))

    if details.get("vote_average") is not None:
        ratings = ET.SubElement(root, "ratings")
        rating = ET.SubElement(
            ratings,
            "rating",
            name="tmdb",
            max="10",
            default="true",
        )
        _el(rating, "value", round(details["vote_average"], 1))
        _el(rating, "votes", details.get("vote_count"))
        _el(root, "rating", round(details["vote_average"], 1))

    # 运行时长(取平均集时长)
    eps_runtime = details.get("episode_run_time") or []
    if eps_runtime:
        _el(root, "runtime", eps_runtime[0])

    for g in details.get("genres") or []:
        _el(root, "genre", g.get("name"))
    for s in details.get("networks") or []:
        _el(root, "studio", s.get("name"))
    for c in details.get("production_companies") or []:
        _el(root, "studio", c.get("name"))
    for c in details.get("origin_country") or []:
        _el(root, "country", c)

    if details.get("poster_path"):
        _el(root, "thumb", f"{IMG_BASE}{details['poster_path']}", aspect="poster")
    if details.get("backdrop_path"):
        fanart = ET.SubElement(root, "fanart")
        _el(fanart, "thumb", f"{IMG_BASE}{details['backdrop_path']}")

    _add_ids(root, details["id"], details.get("external_ids") or {})
    _add_credits(root, details.get("credits") or {})

    return _pretty(root)


def episode_nfo(episode: dict, show: Optional[dict] = None) -> str:
    """生成单集的 episodedetails NFO(放在每个视频文件旁)"""
    root = ET.Element("episodedetails")
    _el(root, "title", episode.get("name") or "")
    _el(root, "season", episode.get("season_number"))
    _el(root, "episode", episode.get("episode_number"))
    _el(root, "plot", episode.get("overview"))
    _el(root, "aired", episode.get("air_date"))
    if episode.get("runtime"):
        _el(root, "runtime", episode["runtime"])
    if episode.get("vote_average") is not None:
        ratings = ET.SubElement(root, "ratings")
        rating = ET.SubElement(
            ratings,
            "rating",
            name="tmdb",
            max="10",
            default="true",
        )
        _el(rating, "value", round(episode["vote_average"], 1))
        _el(rating, "votes", episode.get("vote_count"))
    if episode.get("still_path"):
        _el(root, "thumb", f"{IMG_BASE}{episode['still_path']}")
    if show:
        _el(root, "showtitle", show.get("name"))

    ep_credits = episode.get("credits") or {}
    _add_credits(root, ep_credits)

    _el(root, "uniqueid", episode.get("id"), type="tmdb", default="true")

    return _pretty(root)
