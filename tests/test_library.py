from __future__ import annotations

import unittest

from panbox.clouds.base import RemoteFile
from panbox.library import Layout, extract_media_tags, parse_season_from_name, scan_existing_episodes


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

    def test_extract_media_tags_preserves_high_confidence_technical_info(self) -> None:
        tags = extract_media_tags(
            "Scary.Movie.6.2026.2160p.iT.WEB-DL.DDP5.1.Atmos.DV.HDR.H.265.mkv"
        )

        self.assertEqual(
            tags,
            ["2160p", "WEB-DL", "DDP5.1", "Atmos", "DV", "HDR", "H.265"],
        )

    def test_layout_filenames_append_media_tags_from_source_name(self) -> None:
        source = "Scary.Movie.6.2026.2160p.iT.WEB-DL.DDP5.1.Atmos.DV.HDR.H.265.mkv"
        movie = Layout(title="惊声尖笑6", year="2026", media_type="movie")
        show = Layout(title="Show", year="2026", media_type="tv")

        self.assertEqual(
            movie.movie_filename("mkv", source_name=source),
            "惊声尖笑6 (2026) - 2160p WEB-DL DDP5.1 Atmos DV HDR H.265.mkv",
        )
        self.assertEqual(
            show.tv_filename(1, 2, "mkv", source_name=source),
            "Show - S01E02 - 2160p WEB-DL DDP5.1 Atmos DV HDR H.265.mkv",
        )

    def test_extract_media_tags_keeps_hdr10_plus(self) -> None:
        tags = extract_media_tags("Movie.2026.2160p.WEBRip.HDR10+.HEVC.FLAC.mkv")

        self.assertIn("HDR10+", tags)
        self.assertNotIn("HDR10", tags)


if __name__ == "__main__":
    unittest.main()
