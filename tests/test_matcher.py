from __future__ import annotations

import unittest

from panbox.matcher import Guess, normalize_query, parse_hint


class MatcherTest(unittest.TestCase):
    def test_parse_hint_strips_year_season_episode_and_quality_tags(self) -> None:
        parsed = parse_hint("凡人修仙传 第二季 2024 4K WEB-DL S02E01-E19")

        self.assertEqual(parsed.title, "凡人修仙传")
        self.assertEqual(parsed.year, 2024)
        self.assertEqual(parsed.season, 2)

    def test_normalize_query_extracts_chinese_season(self) -> None:
        query, season = normalize_query("庆余年 第二季")

        self.assertEqual(query, "庆余年")
        self.assertEqual(season, 2)

    def test_chinese_variety_episode_half_maps_to_linear_episode(self) -> None:
        guess = Guess.from_text("快乐再出发 第2期下.mp4")

        self.assertEqual(guess.media_type, "tv")
        self.assertEqual(guess.episode, 4)


if __name__ == "__main__":
    unittest.main()
