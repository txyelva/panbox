from __future__ import annotations

import unittest

from panbox.scraper.tmdb import TMDB


class FakeTMDB(TMDB):
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def _get(self, path: str, **params):
        self.calls.append((path, params))
        if path == "/search/movie":
            return {
                "results": [
                    {
                        "id": 10,
                        "title": "Same Name",
                        "original_title": "Same Name",
                        "release_date": "2024-01-01",
                        "overview": "",
                        "popularity": 3.0,
                    }
                ]
            }
        if path == "/search/tv":
            return {
                "results": [
                    {
                        "id": 20,
                        "name": "Same Name",
                        "original_name": "Same Name",
                        "first_air_date": "2024-02-01",
                        "overview": "",
                        "popularity": 9.0,
                    }
                ]
            }
        if path == "/search/multi":
            return {"results": []}
        raise AssertionError(f"unexpected path {path}")


class TMDBSearchTest(unittest.TestCase):
    def test_auto_media_type_search_uses_year_filtered_movie_and_tv_endpoints(self) -> None:
        tmdb = FakeTMDB()

        results = tmdb.search("Same Name", year=2024, media_type=None)

        self.assertEqual([r.media_type for r in results], ["tv", "movie"])
        self.assertEqual(
            tmdb.calls,
            [
                ("/search/movie", {"query": "Same Name", "year": 2024}),
                ("/search/tv", {"query": "Same Name", "first_air_date_year": 2024}),
            ],
        )


if __name__ == "__main__":
    unittest.main()
