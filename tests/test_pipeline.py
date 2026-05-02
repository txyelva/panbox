from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Iterable

from panbox.clouds.base import RemoteFile
from panbox.config import Config, TMDBConfig
from panbox.library import Layout
from panbox.pipeline import (
    _ensure_staging_season_match,
    _finalize_tv,
    _tmdb_says_variety,
    _tv_library_root,
)


class RenameOnlyCloud:
    def __init__(self) -> None:
        self.children = [RemoteFile(fid="dir1", name="第一季", is_dir=True)]
        self.renamed: list[tuple[str, str]] = []

    def list_dir(self, pdir_fid: str) -> list[RemoteFile]:
        return self.children

    def rename(self, fid: str, new_name: str) -> None:
        self.renamed.append((fid, new_name))


class FakeCloud:
    def __init__(self) -> None:
        self._next = 0
        self.path_to_fid: dict[str, str] = {}
        self.children: dict[str, list[RemoteFile]] = {}
        self.renamed: list[tuple[str, str]] = []
        self.moved: list[tuple[tuple[str, ...], str]] = []

    def mkdir_p(self, path: str) -> str:
        if path in self.path_to_fid:
            return self.path_to_fid[path]

        self._next += 1
        fid = f"dir{self._next}"
        self.path_to_fid[path] = fid
        self.children.setdefault(fid, [])

        parent_path, _, name = path.rstrip("/").rpartition("/")
        if parent_path:
            parent_fid = self.path_to_fid.get(parent_path)
            if parent_fid:
                self.children.setdefault(parent_fid, []).append(
                    RemoteFile(fid=fid, name=name, is_dir=True, parent_fid=parent_fid)
                )
        return fid

    def list_dir(self, pdir_fid: str) -> list[RemoteFile]:
        return list(self.children.get(pdir_fid, []))

    def rename(self, fid: str, new_name: str) -> None:
        self.renamed.append((fid, new_name))

    def move(self, fids: Iterable[str], to_pdir_fid: str) -> None:
        self.moved.append((tuple(fids), to_pdir_fid))


class PipelineTest(unittest.TestCase):
    def test_ensure_staging_season_match_replaces_chinese_season_name(self) -> None:
        cloud = RenameOnlyCloud()

        _ensure_staging_season_match(cloud, "staging", {"dir1"}, season_hint=2)

        self.assertEqual(cloud.renamed, [("dir1", "第2季")])

    def test_finalize_tv_does_not_duplicate_orphan_skipped_names(self) -> None:
        cfg = Config(tmdb=TMDBConfig(api_key="test"))
        cloud_cfg = SimpleNamespace(library_tv="/TV", rejected_dir_tv="")
        layout = Layout(title="Show", year="2024", media_type="tv")
        cloud = FakeCloud()
        staged = [
            (RemoteFile(fid="video1", name="Show.S01E01.mkv", is_dir=False), None),
            (RemoteFile(fid="video2", name="unparsed.video.mkv", is_dir=False), None),
        ]

        result = _finalize_tv(
            cloud, cfg, cloud_cfg, layout, staged, season_hint=None
        )

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.skipped, ["unparsed.video.mkv"])
        self.assertEqual(result.added, ["Show - S01E01.mkv"])

    def test_finalize_tv_can_use_variety_library_root(self) -> None:
        cfg = Config(tmdb=TMDBConfig(api_key="test"))
        cloud_cfg = SimpleNamespace(library_tv="/TV", rejected_dir_tv="")
        layout = Layout(title="奔跑吧", year="2014", media_type="tv")
        cloud = FakeCloud()
        staged = [(RemoteFile(fid="video1", name="奔跑吧.S14E01.mp4", is_dir=False), None)]

        result = _finalize_tv(
            cloud, cfg, cloud_cfg, layout, staged, season_hint=None,
            library_tv_root="/Variety",
        )

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.path, "/Variety/奔跑吧 (2014)/Season 14")
        self.assertEqual(result.added, ["奔跑吧 - S14E01.mp4"])

    def test_tmdb_variety_detection_uses_type_or_genre(self) -> None:
        self.assertTrue(_tmdb_says_variety({"type": "Reality", "genres": []}))
        self.assertTrue(_tmdb_says_variety({"genres": [{"id": 10764, "name": "真人秀"}]}))
        self.assertFalse(_tmdb_says_variety({"type": "Scripted", "genres": [{"id": 18, "name": "剧情"}]}))

    def test_tv_library_root_defaults_variety_to_sibling_folder(self) -> None:
        cloud_cfg = SimpleNamespace(library_tv="/TV", library_variety="")
        self.assertEqual(_tv_library_root(cloud_cfg, is_variety=False), "/TV")
        self.assertEqual(_tv_library_root(cloud_cfg, is_variety=True), "/Variety")
        cloud_cfg.library_variety = "/Variety"
        self.assertEqual(_tv_library_root(cloud_cfg, is_variety=True), "/Variety")


if __name__ == "__main__":
    unittest.main()
