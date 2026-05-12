from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin

import requests

from .config import PansouConfig

API_CLOUD_ALIASES = {
    "115": "115",
    "ali": "aliyun",
    "aliyun": "aliyun",
    "quark": "quark",
    "baidu": "baidu",
}

PANBOX_CLOUD_ALIASES = {
    "115": "115",
    "aliyun": "ali",
    "ali": "ali",
    "quark": "quark",
    "baidu": "baidu",
}


class PansouError(RuntimeError):
    pass


@dataclass(frozen=True)
class PansouCandidate:
    cloud: str
    panbox_cloud: str
    url: str
    password: str = ""
    note: str = ""
    datetime: str = ""
    source: str = ""
    images: list[str] = field(default_factory=list)
    score: int = 0
    signals: list[str] = field(default_factory=list)
    check_state: str = ""
    check_summary: str = ""


@dataclass(frozen=True)
class PansouSearchResult:
    status: str
    query: str
    base_url: str
    total: int
    cloud_types: list[str]
    candidates: list[PansouCandidate]
    message: str = ""


def normalize_api_cloud(cloud: str) -> str:
    key = str(cloud or "").strip().lower()
    if key not in API_CLOUD_ALIASES:
        raise ValueError(f"不支持的 PanSou 网盘类型: {cloud}")
    return API_CLOUD_ALIASES[key]


def normalize_api_clouds(clouds: list[str] | tuple[str, ...] | None) -> list[str]:
    if not clouds:
        clouds = ["115", "aliyun", "quark", "baidu"]
    out: list[str] = []
    for cloud in clouds:
        normalized = normalize_api_cloud(cloud)
        if normalized not in out:
            out.append(normalized)
    return out


def panbox_cloud_for(api_cloud: str) -> str:
    return PANBOX_CLOUD_ALIASES.get(str(api_cloud or "").strip().lower(), api_cloud)


def _parse_datetime(value: str) -> datetime | None:
    if not value or value.startswith("0001-"):
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _episode_range_bonus(text: str, signals: list[str]) -> int:
    score = 0
    if re.search(r"(全集|全\s*\d{1,3}\s*[集期]|完结|完整不缺集|不缺集)", text):
        signals.append("complete")
        score += 35

    m = re.search(r"S\d{1,2}\s*E\s*(\d{1,3})\s*(?:-|~|到|至)\s*(?:E\s*)?(\d{1,3})", text, re.I)
    if m:
        start, end = int(m.group(1)), int(m.group(2))
        if end >= start:
            count = end - start + 1
            signals.append(f"episode_range:{start}-{end}")
            score += min(35, 12 + count)

    m = re.search(r"(?:更至|更新至|更新到|更新|更)\s*(?:第)?\s*(\d{1,3})\s*[集期]", text)
    if m:
        count = int(m.group(1))
        signals.append(f"updated_to:{count}")
        score += min(35, 10 + count // 2)

    has_episode_query = bool(re.search(r"\bS\d{1,2}\s*E\d{1,3}\b|\bE\d{1,3}\b|第\s*\d{1,3}\s*[集期]", text, re.I))
    is_single_episode = bool(re.search(r"\bS\d{1,2}\s*E\d{1,3}\b", text, re.I))
    has_collection_signal = any(s.startswith(("updated_to", "episode_range")) or s == "complete" for s in signals)
    if is_single_episode and not has_collection_signal and has_episode_query:
        signals.append("single_episode")
        score -= 12
    return score


def score_candidate(query: str, cloud_rank: int, candidate: dict[str, Any]) -> tuple[int, list[str]]:
    note = str(candidate.get("note") or "")
    text = note.lower()
    query_text = query.strip()
    signals: list[str] = []
    score = max(0, 12 - cloud_rank * 2)

    compact_note = re.sub(r"\s+", "", note).lower()
    compact_query = re.sub(r"\s+", "", query_text).lower()
    if compact_query and compact_query in compact_note:
        signals.append("title_match")
        score += 40

    year_match = re.search(r"(19|20)\d{2}", query_text)
    if year_match and year_match.group(0) in note:
        signals.append("year_match")
        score += 8

    score += _episode_range_bonus(text, signals)

    dt = _parse_datetime(str(candidate.get("datetime") or ""))
    if dt:
        now = datetime.now(timezone.utc)
        age_days = max(0, (now - dt.astimezone(timezone.utc)).days)
        if age_days <= 3:
            signals.append("very_fresh")
            score += 10
        elif age_days <= 30:
            signals.append("fresh")
            score += 6
        elif age_days <= 180:
            signals.append("recent")
            score += 3

    if str(candidate.get("source") or "").startswith("plugin:"):
        signals.append("plugin")
        score += 2

    return score, signals


class PansouClient:
    def __init__(
        self,
        config: PansouConfig | None = None,
        *,
        base_url: str | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self.config = config or PansouConfig()
        self.base_url = (base_url or self.config.base_url).rstrip("/")
        self.session = session or requests.Session()

    def search(
        self,
        query: str,
        *,
        cloud_types: list[str] | tuple[str, ...] | None = None,
        max_results: int | None = None,
        refresh: bool = False,
        check_links: bool = False,
    ) -> PansouSearchResult:
        if not query.strip():
            raise PansouError("搜索关键词不能为空")

        clouds = normalize_api_clouds(list(cloud_types) if cloud_types else self.config.cloud_types)
        limit = max_results if max_results is not None else self.config.max_results
        payload = {
            "kw": query.strip(),
            "cloud_types": clouds,
            "res": "merge",
            "src": "all",
        }
        if refresh:
            payload["refresh"] = True

        raw = self._post_json("/api/search", payload)
        code = raw.get("code")
        if code not in (None, 0):
            raise PansouError(str(raw.get("message") or raw.get("error") or raw))
        data = raw.get("data") if isinstance(raw.get("data"), dict) else raw
        merged = data.get("merged_by_type") or {}
        candidates = self._flatten_candidates(query, clouds, merged)
        candidates = candidates[: max(0, int(limit or 0))]
        if check_links and candidates:
            try:
                candidates = self.check_links(candidates)
            except PansouError as exc:
                candidates = [
                    replace(candidate, check_state="unavailable", check_summary=str(exc))
                    for candidate in candidates
                ]
        return PansouSearchResult(
            status="ok",
            query=query.strip(),
            base_url=self.base_url,
            total=int(data.get("total") or len(candidates)),
            cloud_types=clouds,
            candidates=candidates,
            message="请选择候选资源后,再用 panbox ingest 对该链接执行 dry-run。",
        )

    def check_links(self, candidates: list[PansouCandidate]) -> list[PansouCandidate]:
        items = [
            {
                "disk_type": c.cloud,
                "url": c.url,
                "password": c.password,
            }
            for c in candidates
        ]
        raw = self._post_json("/api/check/links", {"items": items})
        rows = raw.get("results") or raw.get("data", {}).get("results") or []
        by_url = {str(row.get("url") or ""): row for row in rows}
        checked: list[PansouCandidate] = []
        for candidate in candidates:
            row = by_url.get(candidate.url) or {}
            checked.append(
                replace(
                    candidate,
                    check_state=str(row.get("state") or ""),
                    check_summary=str(row.get("summary") or ""),
                    url=str(row.get("normalized_url") or candidate.url),
                )
            )
        return checked

    def _post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = urljoin(self.base_url + "/", path.lstrip("/"))
        try:
            response = self.session.post(url, json=payload, timeout=self.config.timeout)
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as exc:
            raise PansouError(f"PanSou 请求失败: {exc}") from exc
        except ValueError as exc:
            raise PansouError("PanSou 返回的不是 JSON") from exc
        if not isinstance(data, dict):
            raise PansouError("PanSou 返回格式异常")
        return data

    def _flatten_candidates(
        self,
        query: str,
        clouds: list[str],
        merged: dict[str, Any],
    ) -> list[PansouCandidate]:
        rows: list[PansouCandidate] = []
        seen: set[tuple[str, str]] = set()
        cloud_rank = {cloud: idx for idx, cloud in enumerate(clouds)}
        for cloud in clouds:
            for item in merged.get(cloud) or []:
                if not isinstance(item, dict):
                    continue
                url = str(item.get("url") or "").strip()
                if not url:
                    continue
                password = str(item.get("password") or "")
                key = (url, password)
                if key in seen:
                    continue
                seen.add(key)
                score, signals = score_candidate(query, cloud_rank.get(cloud, 99), item)
                images = item.get("images") if isinstance(item.get("images"), list) else []
                rows.append(
                    PansouCandidate(
                        cloud=cloud,
                        panbox_cloud=panbox_cloud_for(cloud),
                        url=url,
                        password=password,
                        note=str(item.get("note") or ""),
                        datetime=str(item.get("datetime") or ""),
                        source=str(item.get("source") or ""),
                        images=[str(img) for img in images],
                        score=score,
                        signals=signals,
                    )
                )
        return sorted(rows, key=lambda c: c.score, reverse=True)
