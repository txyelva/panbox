from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from click.testing import CliRunner

from panbox import cli as cli_mod
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


if __name__ == "__main__":
    unittest.main()
