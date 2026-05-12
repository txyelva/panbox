from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from click.testing import CliRunner

from panbox import cli as cli_mod
from panbox.config import PansouConfig
from panbox.pansou import PansouCandidate, PansouSearchResult
from panbox.scraper.tmdb import TMDBResult


class FakeTMDB:
    def __init__(self, api_key: str, language: str) -> None:
        self.api_key = api_key
        self.language = language

    def search(self, query: str, year=None, media_type=None):
        return [
            TMDBResult(
                id=123,
                media_type="tv",
                title="Show",
                original_title="Show",
                year="2024",
                overview="",
                popularity=1.0,
                poster_path=None,
            )
        ]


class CLITest(unittest.TestCase):
    def test_identify_file_json_serializes_guess(self) -> None:
        cfg = SimpleNamespace(tmdb=SimpleNamespace(api_key="key", language="zh-CN"))
        runner = CliRunner()

        with patch.object(cli_mod.Config, "load", return_value=cfg), patch.object(
            cli_mod, "TMDB", FakeTMDB
        ):
            result = runner.invoke(
                cli_mod.main, ["identify", "--file", "Show.S01E01.mkv", "--json"]
            )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["guess"]["season"], 1)
        self.assertEqual(payload["candidates"][0]["tmdb_id"], 123)

    def test_search_json_returns_indexed_candidates(self) -> None:
        cfg = SimpleNamespace(pansou=PansouConfig(base_url="https://example.test"))

        class FakePansouClient:
            def __init__(self, config, base_url=None):
                self.config = config
                self.base_url = base_url

            def search(self, query, cloud_types=None, max_results=None, refresh=False, check_links=False):
                return PansouSearchResult(
                    status="ok",
                    query=query,
                    base_url="https://example.test",
                    total=1,
                    cloud_types=["quark"],
                    candidates=[
                        PansouCandidate(
                            cloud="quark",
                            panbox_cloud="quark",
                            url="https://pan.quark.cn/s/demo",
                            note="Demo",
                            score=42,
                        )
                    ],
                )

        runner = CliRunner()
        with patch.object(cli_mod.Config, "load", return_value=cfg), patch.object(
            cli_mod, "PansouClient", FakePansouClient
        ):
            result = runner.invoke(
                cli_mod.main, ["search", "Demo", "--cloud", "quark", "--json"]
            )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["candidates"][0]["index"], 1)
        self.assertEqual(payload["candidates"][0]["url"], "https://pan.quark.cn/s/demo")
        self.assertEqual(payload["cloud_source"], "explicit")

    def test_search_defaults_to_configured_cloud_credentials(self) -> None:
        cfg = SimpleNamespace(
            pansou=PansouConfig(
                base_url="https://example.test",
                cloud_types=["baidu", "115", "quark", "aliyun"],
            ),
            drive115=SimpleNamespace(cookie="UID=1"),
            ali=SimpleNamespace(refresh_token=""),
            quark=SimpleNamespace(cookie="QUARK=1"),
            baidu=SimpleNamespace(cookie=""),
        )

        class FakePansouClient:
            def __init__(self, config, base_url=None):
                self.config = config
                self.base_url = base_url

            def search(self, query, cloud_types=None, max_results=None, refresh=False, check_links=False):
                return PansouSearchResult(
                    status="ok",
                    query=query,
                    base_url="https://example.test",
                    total=0,
                    cloud_types=list(cloud_types or []),
                    candidates=[],
                )

        runner = CliRunner()
        with patch.object(cli_mod.Config, "load", return_value=cfg), patch.object(
            cli_mod, "PansouClient", FakePansouClient
        ):
            result = runner.invoke(cli_mod.main, ["search", "Demo", "--json"])

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["cloud_types"], ["115", "quark"])
        self.assertEqual(payload["cloud_source"], "configured")

    def test_search_requires_cloud_when_no_configured_credentials(self) -> None:
        cfg = SimpleNamespace(
            pansou=PansouConfig(),
            drive115=SimpleNamespace(cookie=""),
            ali=SimpleNamespace(refresh_token=""),
            quark=SimpleNamespace(cookie=""),
            baidu=SimpleNamespace(cookie=""),
        )
        runner = CliRunner()

        with patch.object(cli_mod.Config, "load", return_value=cfg):
            result = runner.invoke(cli_mod.main, ["search", "Demo", "--json"])

        self.assertEqual(result.exit_code, 1, result.output)
        payload = json.loads(result.output)
        self.assertIn("未指定 --cloud", payload["error"])


if __name__ == "__main__":
    unittest.main()
