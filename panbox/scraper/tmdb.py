from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import requests

BASE = "https://api.themoviedb.org/3"
IMG = "https://image.tmdb.org/t/p/original"


@dataclass
class TMDBResult:
    id: int
    media_type: str
    title: str
    original_title: str
    year: Optional[str]
    overview: str
    popularity: float
    poster_path: Optional[str]

    @property
    def poster_url(self) -> Optional[str]:
        return f"{IMG}{self.poster_path}" if self.poster_path else None


class TMDB:
    def __init__(self, api_key: str, language: str = "zh-CN", timeout: int = 15):
        self.api_key = api_key
        self.language = language
        self.timeout = timeout
        self.session = requests.Session()

    def _get(self, path: str, **params):
        params.setdefault("api_key", self.api_key)
        params.setdefault("language", self.language)
        r = self.session.get(f"{BASE}{path}", params=params, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def search(
        self,
        query: str,
        year: Optional[int] = None,
        media_type: Optional[str] = None,
    ) -> list[TMDBResult]:
        if media_type == "movie":
            data = self._get("/search/movie", query=query, year=year)
            return [self._movie(x) for x in data.get("results", [])]
        if media_type == "tv":
            data = self._get("/search/tv", query=query, first_air_date_year=year)
            return [self._tv(x) for x in data.get("results", [])]
        if year is not None:
            movie_data = self._get("/search/movie", query=query, year=year)
            tv_data = self._get("/search/tv", query=query, first_air_date_year=year)
            out = [self._movie(x) for x in movie_data.get("results", [])]
            out.extend(self._tv(x) for x in tv_data.get("results", []))
            if out:
                return sorted(out, key=lambda x: x.popularity, reverse=True)
        data = self._get("/search/multi", query=query)
        out: list[TMDBResult] = []
        for x in data.get("results", []):
            t = x.get("media_type")
            if t == "movie":
                out.append(self._movie(x))
            elif t == "tv":
                out.append(self._tv(x))
        return out

    def movie_details(self, tmdb_id: int) -> dict:
        return self._get(
            f"/movie/{tmdb_id}",
            append_to_response="credits,external_ids,release_dates",
        )

    def tv_details(self, tmdb_id: int) -> dict:
        return self._get(
            f"/tv/{tmdb_id}",
            append_to_response="credits,external_ids,content_ratings",
        )

    def tv_season(self, tmdb_id: int, season: int) -> dict:
        return self._get(
            f"/tv/{tmdb_id}/season/{season}",
            append_to_response="credits",
        )

    def tv_episode(self, tmdb_id: int, season: int, episode: int) -> dict:
        return self._get(
            f"/tv/{tmdb_id}/season/{season}/episode/{episode}",
            append_to_response="credits",
        )

    def ping(self) -> bool:
        try:
            self._get("/configuration")
            return True
        except Exception:
            return False

    @staticmethod
    def _movie(x: dict) -> TMDBResult:
        date = x.get("release_date") or ""
        return TMDBResult(
            id=x["id"],
            media_type="movie",
            title=x.get("title") or x.get("original_title") or "",
            original_title=x.get("original_title", ""),
            year=(date.split("-")[0] if date else None),
            overview=x.get("overview", ""),
            popularity=float(x.get("popularity", 0.0)),
            poster_path=x.get("poster_path"),
        )

    @staticmethod
    def _tv(x: dict) -> TMDBResult:
        date = x.get("first_air_date") or ""
        return TMDBResult(
            id=x["id"],
            media_type="tv",
            title=x.get("name") or x.get("original_name") or "",
            original_title=x.get("original_name", ""),
            year=(date.split("-")[0] if date else None),
            overview=x.get("overview", ""),
            popularity=float(x.get("popularity", 0.0)),
            poster_path=x.get("poster_path"),
        )
