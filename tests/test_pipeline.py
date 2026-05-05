from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Iterable
from unittest.mock import patch

from panbox.clouds.base import RemoteFile
from panbox.config import Config, TMDBConfig
from panbox.library import Layout
from panbox.pipeline import (
    _ensure_staging_season_match,
    _finalize_tv,
    _apply_rename_rules,
    _plan_tv_targets,
    scrape_folder,
    _tmdb_says_variety,
    _tv_library_root,
    normalize_rename_plan,
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
        self.uploaded: list[tuple[str, str, str]] = []

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

    def resolve_path(self, path: str) -> str | None:
        return self.path_to_fid.get(path.rstrip("/") or "/")

    def rename(self, fid: str, new_name: str) -> None:
        self.renamed.append((fid, new_name))
        for children in self.children.values():
            for idx, child in enumerate(children):
                if child.fid == fid:
                    children[idx] = RemoteFile(
                        fid=child.fid,
                        name=new_name,
                        is_dir=child.is_dir,
                        size=child.size,
                        parent_fid=child.parent_fid,
                        fid_token=child.fid_token,
                    )

    def move(self, fids: Iterable[str], to_pdir_fid: str) -> None:
        self.moved.append((tuple(fids), to_pdir_fid))

    def delete(self, fids: Iterable[str]) -> None:
        doomed = set(fids)
        for parent, children in list(self.children.items()):
            self.children[parent] = [c for c in children if c.fid not in doomed]

    def upload_bytes(
        self,
        parent_fid: str,
        name: str,
        data: bytes,
        mime: str = "application/octet-stream",
    ) -> str:
        self._next += 1
        fid = f"file{self._next}"
        self.children.setdefault(parent_fid, []).append(
            RemoteFile(fid=fid, name=name, is_dir=False, parent_fid=parent_fid)
        )
        self.uploaded.append((parent_fid, name, mime))
        return fid


class FakeTMDB:
    def __init__(self, api_key: str, language: str) -> None:
        self.api_key = api_key
        self.language = language

    def tv_details(self, tmdb_id: int) -> dict:
        return {
            "id": tmdb_id,
            "name": "Show",
            "original_name": "Show",
            "first_air_date": "2024-01-01",
            "overview": "",
            "genres": [],
            "external_ids": {},
            "credits": {},
        }

    def tv_episode(self, tmdb_id: int, season: int, episode: int) -> dict:
        return {
            "id": 9000 + episode,
            "name": f"Episode {episode}",
            "season_number": season,
            "episode_number": episode,
            "overview": "",
            "air_date": "2024-01-01",
            "credits": {},
        }


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

    def test_rename_plan_updates_virtual_staged_names(self) -> None:
        staged = [
            (RemoteFile(fid="v1", name="20260505.mp4", is_dir=False), None),
        ]
        rules = normalize_rename_plan({
            "20260505.mp4": "Show - S01E03.mp4",
        })

        renamed_staged, rows = _apply_rename_rules(
            None, staged, rules, dry_run=True
        )

        self.assertEqual(renamed_staged[0][0].name, "Show - S01E03.mp4")
        self.assertEqual(rows[0]["source"], "20260505.mp4")
        self.assertEqual(rows[0]["target"], "Show - S01E03.mp4")

    def test_plan_tv_targets_uses_renamed_names(self) -> None:
        layout = Layout(title="Show", year="2026", media_type="tv")
        staged = [
            (RemoteFile(fid="v1", name="Show - S01E03.mp4", is_dir=False), None),
        ]

        added, skipped = _plan_tv_targets(layout, staged, season_hint=None)

        self.assertEqual(added, ["Show - S01E03.mp4"])
        self.assertEqual(skipped, [])

    def test_scrape_folder_backfills_existing_tv_metadata(self) -> None:
        cfg = Config(tmdb=TMDBConfig(api_key="test"))
        cloud = FakeCloud()
        show_fid = cloud.mkdir_p("/TV/Show (2024)")
        season_fid = cloud.mkdir_p("/TV/Show (2024)/Season 01")
        cloud.children[season_fid].append(
            RemoteFile(
                fid="video1",
                name="Show - S01E01.mkv",
                is_dir=False,
                parent_fid=season_fid,
            )
        )

        with patch("panbox.pipeline.cloud_by_name", return_value=cloud), patch(
            "panbox.pipeline.TMDB", FakeTMDB
        ):
            result = scrape_folder(
                cfg,
                "115",
                "/TV/Show (2024)",
                tmdb_id=123,
                media_type="tv",
                season=1,
            )

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.path, "/TV/Show (2024)")
        self.assertEqual(result.skipped, [])
        self.assertEqual(
            {(row["name"], row["status"]) for row in result.metadata},
            {
                ("tvshow.nfo", "created"),
                ("Show - S01E01.nfo", "created"),
            },
        )
        self.assertIn((show_fid, "tvshow.nfo", "application/xml"), cloud.uploaded)
        self.assertIn((season_fid, "Show - S01E01.nfo", "application/xml"), cloud.uploaded)


if __name__ == "__main__":
    unittest.main()
