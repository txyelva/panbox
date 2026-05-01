from __future__ import annotations

import unittest

from panbox.clouds.base import RemoteFile
from panbox.library import parse_season_from_name, scan_existing_episodes


class LibraryTest(unittest.TestCase):
    def test_parse_season_from_common_folder_names(self) -> None:
        cases = {
            "S03": 3,
            "Season_02": 2,
            "第1季": 1,
            "第一季": 1,
            "第十一季": 11,
        }

        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(parse_season_from_name(name), expected)

    def test_scan_existing_episodes_handles_common_episode_patterns(self) -> None:
        files = [
            RemoteFile(fid="1", name="Show.S01E02.mkv", is_dir=False),
            RemoteFile(fid="2", name="Show 1x03.mp4", is_dir=False),
            RemoteFile(fid="3", name="Show.S02E01.mkv", is_dir=False),
            RemoteFile(fid="4", name="poster.jpg", is_dir=False),
        ]

        self.assertEqual(scan_existing_episodes(files, season=1), {2, 3})


if __name__ == "__main__":
    unittest.main()
