from __future__ import annotations

import unittest

from panbox.config import PansouConfig
from panbox.pansou import PansouClient, normalize_api_clouds


class FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"{self.status_code} error")
        return None

    def json(self) -> dict:
        return self.payload


class FakeSession:
    def __init__(self, payload: dict | list[FakeResponse]) -> None:
        self.payload = payload
        self.calls: list[dict] = []

    def post(self, url: str, json: dict, timeout: int):
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        if isinstance(self.payload, list):
            return self.payload.pop(0)
        return FakeResponse(self.payload)


class PansouTest(unittest.TestCase):
    def test_normalize_cloud_aliases(self) -> None:
        self.assertEqual(normalize_api_clouds(["ali", "115", "aliyun"]), ["aliyun", "115"])

    def test_search_ranks_collection_above_single_episode(self) -> None:
        payload = {
            "code": 0,
            "data": {
                "total": 2,
                "merged_by_type": {
                    "115": [
                        {
                            "url": "https://115cdn.com/s/one?password=abcd",
                            "password": "abcd",
                            "note": "爱情没有神话 (2026) S01E30 4K WEB-DL",
                            "datetime": "2026-05-12T03:02:58Z",
                            "source": "tg:demo",
                        }
                    ],
                    "quark": [
                        {
                            "url": "https://pan.quark.cn/s/full",
                            "password": "",
                            "note": "爱情没有神话【2026国剧】更新至第30集 完整不缺集 4K",
                            "datetime": "2026-05-12T01:16:56Z",
                            "source": "tg:demo",
                        }
                    ],
                },
            },
        }
        session = FakeSession(payload)
        client = PansouClient(PansouConfig(timeout=9), session=session)

        result = client.search("爱情没有神话", cloud_types=["115", "quark"], max_results=2)

        self.assertEqual(result.candidates[0].cloud, "quark")
        self.assertIn("updated_to:30", result.candidates[0].signals)
        self.assertEqual(session.calls[0]["json"]["cloud_types"], ["115", "quark"])
        self.assertEqual(session.calls[0]["timeout"], 9)

    def test_check_links_failure_marks_candidates_unavailable(self) -> None:
        search_payload = {
            "code": 0,
            "data": {
                "total": 1,
                "merged_by_type": {
                    "quark": [
                        {
                            "url": "https://pan.quark.cn/s/full",
                            "note": "爱情没有神话 更新至第30集",
                        }
                    ]
                },
            },
        }
        session = FakeSession([FakeResponse(search_payload), FakeResponse({}, status_code=404)])
        client = PansouClient(PansouConfig(), session=session)

        result = client.search("爱情没有神话", cloud_types=["quark"], check_links=True)

        self.assertEqual(result.candidates[0].check_state, "unavailable")
        self.assertIn("PanSou 请求失败", result.candidates[0].check_summary)


if __name__ == "__main__":
    unittest.main()
