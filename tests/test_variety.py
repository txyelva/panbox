from __future__ import annotations

import unittest
from datetime import date

from panbox.clouds.base import RemoteFile
from panbox.variety import (
    VarietyEpisode,
    build_variety_episodes,
    extract_period_part,
    match_variety_files,
    parse_date,
)


def video(fid: str, name: str) -> RemoteFile:
    return RemoteFile(fid=fid, name=name, is_dir=False)


class VarietyMatcherTest(unittest.TestCase):
    def test_parse_date_variants(self) -> None:
        self.assertEqual(parse_date("20260424_第1期.mp4"), date(2026, 4, 24))
        self.assertEqual(parse_date("26.04.24 第1期.mp4"), date(2026, 4, 24))

    def test_extract_period_part_variants(self) -> None:
        self.assertEqual(extract_period_part("第1期：初舞台（上）"), (1, "上"))
        self.assertEqual(extract_period_part("20260404期-第1期下.mp4"), (1, "下"))
        self.assertEqual(extract_period_part("第十一期"), (11, None))

    def test_running_man_matches_date_period_and_rejects_extras(self) -> None:
        episodes = [
            VarietyEpisode(1, "夸下海口大对决", date(2026, 4, 24), None, None, ("夸下海口大对决",)),
            VarietyEpisode(2, "第 2 集", date(2026, 5, 1), 2, None, ()),
        ]
        files = [
            video("1", "20260501_第2期.mp4"),
            video("2", "20260427_第1期加更.mkv"),
            video("3", "20260424_第1期：跑男团X时代少年团撕名牌大乱斗.mp4"),
            video("4", "发布会.mp4"),
            video("5", "20250725_精华版.mp4"),
        ]

        matches = match_variety_files(files, episodes)

        self.assertEqual(
            [(m.episode.number, m.file.name) for m in matches],
            [
                (1, "20260424_第1期：跑男团X时代少年团撕名牌大乱斗.mp4"),
                (2, "20260501_第2期.mp4"),
            ],
        )

    def test_ride_the_wind_matches_parts_and_rejects_old_season(self) -> None:
        episodes = build_variety_episodes(
            {
                "episodes": [
                    {"episode_number": 1, "name": "第1期：初舞台（上）", "air_date": "2026-04-03"},
                    {"episode_number": 2, "name": "第1期：初舞台（下）", "air_date": "2026-04-04"},
                    {"episode_number": 3, "name": "第2期：一公车轮排位赛（上）", "air_date": "2026-04-10"},
                ]
            }
        )
        files = [
            video("1", "20260403期-第1期上.mp4"),
            video("2", "20260405期-第1期下.mp4"),
            video("3", "20260406期-加更版第1期.mp4"),
            video("4", "20260410期-第2期上.mp4"),
            video("5", "20250321期-第1期上.mp4"),
        ]

        matches = match_variety_files(files, episodes)

        self.assertEqual(
            [(m.episode.number, m.file.name) for m in matches],
            [
                (1, "20260403期-第1期上.mp4"),
                (2, "20260405期-第1期下.mp4"),
                (3, "20260410期-第2期上.mp4"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
