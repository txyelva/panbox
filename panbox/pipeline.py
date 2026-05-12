from __future__ import annotations

import re
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Optional

from .clouds import from_url as cloud_from_url
from .clouds import by_name as cloud_by_name
from .clouds import parse_share_url as cloud_parse_share_url
from .clouds.base import Cloud, RemoteFile
from .config import Config
from .library import (
    Layout,
    find_season_folder,
    parse_season_from_name,
    scan_existing_episodes,
)
from .matcher import Guess, parse_hint
from .scraper import artwork
from .scraper import nfo as nfo_mod
from .scraper.tmdb import TMDB, TMDBResult
from .variety import build_variety_episodes, match_variety_files

_TMDB_VARIETY_GENRE_IDS = {10764}
_TMDB_VARIETY_TYPES = {"reality"}
_TMDB_VARIETY_GENRE_NAMES = {"reality", "真人秀", "综艺"}


@dataclass
class IngestResult:
    status: str                               # ok | need_confirm | error | skipped
    type: Optional[str] = None                # movie | tv
    tmdb_id: Optional[int] = None
    title: Optional[str] = None
    year: Optional[str] = None
    path: Optional[str] = None
    added: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    skipped_details: list[dict] = field(default_factory=list)
    candidates: list[dict] = field(default_factory=list)
    planned: list[dict] = field(default_factory=list)
    renamed: list[dict] = field(default_factory=list)
    metadata: list[dict] = field(default_factory=list)
    message: Optional[str] = None


@dataclass
class QueryPick:
    query: str
    year: Optional[int]
    season: Optional[int]
    media_type: Optional[str]


@dataclass(frozen=True)
class RenameRule:
    source: Optional[str]
    target: str
    fid: Optional[str] = None


@dataclass(frozen=True)
class ScrapeVideo:
    file: RemoteFile
    folder_season: Optional[int]
    parent_fid: str
    parent_path: str


def _skip_detail(name: str, reason: str, **extra: Any) -> dict:
    row = {"name": name, "reason": reason}
    row.update({k: v for k, v in extra.items() if v is not None})
    return row


def _tmdb_says_variety(details: dict[str, Any]) -> bool:
    tv_type = str(details.get("type") or "").strip().lower()
    if tv_type in _TMDB_VARIETY_TYPES:
        return True
    for genre in details.get("genres") or []:
        gid = genre.get("id")
        name = str(genre.get("name") or "").strip().lower()
        if gid in _TMDB_VARIETY_GENRE_IDS or name in _TMDB_VARIETY_GENRE_NAMES:
            return True
    return False


def _tv_library_root(cloud_cfg: Any, *, is_variety: bool) -> str:
    if not is_variety:
        return cloud_cfg.library_tv
    configured = str(getattr(cloud_cfg, "library_variety", "") or "").strip()
    if configured:
        return configured
    tv_root = cloud_cfg.library_tv.rstrip("/")
    parent, _, _ = tv_root.rpartition("/")
    if parent:
        return f"{parent}/Variety"
    return "/Variety" if tv_root.startswith("/") else "Variety"


def _cloud_config(cfg: Config, cloud_name: str) -> Any:
    attr = {"115": "drive115"}.get(cloud_name, cloud_name)
    if not hasattr(cfg, attr):
        raise ValueError(f"未知云盘:{cloud_name}")
    return getattr(cfg, attr)


def _tmdb_result_from_details(
    tmdb: TMDB,
    tmdb_id: int,
    media_type: Optional[str],
) -> tuple[TMDBResult, dict[str, Any]]:
    if media_type == "movie":
        details = tmdb.movie_details(tmdb_id)
        return (
            TMDBResult(
                id=tmdb_id,
                media_type="movie",
                title=details.get("title") or details.get("original_title") or "",
                original_title=details.get("original_title") or "",
                year=(details.get("release_date") or "").split("-")[0] or None,
                overview=details.get("overview") or "",
                popularity=float(details.get("popularity") or 0.0),
                poster_path=details.get("poster_path"),
            ),
            details,
        )

    details = tmdb.tv_details(tmdb_id)
    return (
        TMDBResult(
            id=tmdb_id,
            media_type="tv",
            title=details.get("name") or details.get("original_name") or "",
            original_title=details.get("original_name") or "",
            year=(details.get("first_air_date") or "").split("-")[0] or None,
            overview=details.get("overview") or "",
            popularity=float(details.get("popularity") or 0.0),
            poster_path=details.get("poster_path"),
        ),
        details,
    )


def normalize_rename_plan(plan: Any) -> list[RenameRule]:
    if not plan:
        return []
    if isinstance(plan, dict):
        raw_items = [
            {"source": str(source), "target": str(target)}
            for source, target in plan.items()
        ]
    elif isinstance(plan, list):
        raw_items = plan
    else:
        raise ValueError("rename plan 必须是对象映射或数组")

    rules: list[RenameRule] = []
    seen: set[tuple[Optional[str], Optional[str]]] = set()
    targets: set[str] = set()
    for item in raw_items:
        if not isinstance(item, dict):
            raise ValueError("rename plan 数组项必须是对象")
        source = item.get("source") or item.get("from") or item.get("name")
        fid = item.get("fid")
        target = item.get("target") or item.get("to") or item.get("new_name")
        source = str(source).strip() if source is not None else None
        fid = str(fid).strip() if fid is not None else None
        target = str(target).strip() if target is not None else ""
        if not source and not fid:
            raise ValueError("rename plan 每项必须提供 source 或 fid")
        if not target:
            raise ValueError("rename plan 每项必须提供 target")
        if "/" in target or "\\" in target:
            raise ValueError(f"rename target 只能是文件名,不能含路径:{target}")
        key = (source, fid)
        if key in seen:
            raise ValueError(f"rename plan source/fid 重复:{source or fid}")
        if target in targets:
            raise ValueError(f"rename plan target 重复:{target}")
        seen.add(key)
        targets.add(target)
        rules.append(RenameRule(source=source, fid=fid, target=target))
    return rules


def _apply_rename_rules(
    qc: Optional[Cloud],
    staged: list[tuple[RemoteFile, Optional[int]]],
    rules: list[RenameRule],
    *,
    dry_run: bool,
) -> tuple[list[tuple[RemoteFile, Optional[int]]], list[dict]]:
    if not rules:
        return staged, []

    by_name: dict[str, list[int]] = {}
    by_fid: dict[str, int] = {}
    for idx, (file, _) in enumerate(staged):
        by_name.setdefault(file.name, []).append(idx)
        by_fid[file.fid] = idx

    out = list(staged)
    rows: list[dict] = []
    used: set[int] = set()
    for rule in rules:
        if rule.fid:
            idx = by_fid.get(rule.fid)
            if idx is None:
                raise ValueError(f"rename plan 未找到 fid:{rule.fid}")
        else:
            matches = by_name.get(rule.source or "", [])
            if not matches:
                raise ValueError(f"rename plan 未找到源文件:{rule.source}")
            if len(matches) > 1:
                raise ValueError(f"rename plan 源文件名不唯一,请使用 fid:{rule.source}")
            idx = matches[0]
        if idx in used:
            raise ValueError(f"rename plan 重复命中同一文件:{rule.source or rule.fid}")
        used.add(idx)
        file, folder_season = out[idx]
        old_name = file.name
        if not dry_run and old_name != rule.target:
            if qc is None:
                raise ValueError("实际重命名需要 cloud client")
            qc.rename(file.fid, rule.target)
        out[idx] = (replace(file, name=rule.target), folder_season)
        rows.append({
            "fid": file.fid,
            "source": old_name,
            "target": rule.target,
            "dry_run": dry_run,
        })
    return out, rows


def _collect_videos_from_drive_folder(
    qc: Cloud,
    folder_fid: str,
    folder_path: str,
) -> list[tuple[RemoteFile, Optional[int]]]:
    out: list[tuple[RemoteFile, Optional[int]]] = []
    root_season = parse_season_from_name(folder_path.rstrip("/").rsplit("/", 1)[-1])

    def walk(fid: str, inherited: Optional[int]) -> None:
        for f in qc.list_dir(fid):
            if f.is_dir:
                sub = parse_season_from_name(f.name)
                walk(f.fid, sub if sub is not None else inherited)
            elif f.is_video:
                out.append((f, inherited))

    walk(folder_fid, root_season)
    return out


def _join_cloud_path(parent: str, name: str) -> str:
    original = parent
    parent = parent.rstrip("/")
    if not parent:
        return f"/{name}" if original.startswith("/") else name
    return f"{parent}/{name}"


def _parent_cloud_path(path: str) -> str:
    stripped = path.rstrip("/")
    if not stripped or stripped == "/":
        return "/"
    parent, _, _ = stripped.rpartition("/")
    return parent or "/"


def _collect_scrape_videos(
    qc: Cloud,
    folder_fid: str,
    folder_path: str,
) -> list[ScrapeVideo]:
    """Collect videos with their actual parent folder, for in-place scraping."""
    out: list[ScrapeVideo] = []
    root_path = folder_path.rstrip("/") or "/"
    root_season = parse_season_from_name(root_path.rsplit("/", 1)[-1])

    def walk(fid: str, path: str, inherited: Optional[int]) -> None:
        for f in qc.list_dir(fid):
            if f.is_dir:
                sub = parse_season_from_name(f.name)
                walk(
                    f.fid,
                    _join_cloud_path(path, f.name),
                    sub if sub is not None else inherited,
                )
            elif f.is_video:
                out.append(
                    ScrapeVideo(
                        file=replace(f, parent_fid=f.parent_fid or fid),
                        folder_season=inherited,
                        parent_fid=f.parent_fid or fid,
                        parent_path=path,
                    )
                )

    walk(folder_fid, root_path, root_season)
    return out


def _plan_tv_targets(
    layout: Layout,
    staged: list[tuple[RemoteFile, Optional[int]]],
    season_hint: Optional[int],
) -> tuple[list[str], list[str], list[dict]]:
    has_multi_folder_season = len({fs for _, fs in staged if fs is not None}) > 1
    added: list[str] = []
    skipped: list[str] = []
    skipped_details: list[dict] = []
    for v, folder_season in staged:
        g = Guess.from_text(v.name)
        season_num = g.season if g.season is not None else folder_season
        if season_num is None:
            season_num = season_hint
        episode = g.episode
        if episode is None:
            skipped.append(v.name)
            skipped_details.append(_skip_detail(v.name, "unparsed_episode"))
            continue
        if season_num is None:
            if has_multi_folder_season:
                skipped.append(v.name)
                skipped_details.append(_skip_detail(v.name, "missing_season"))
                continue
            season_num = 1
        ep_list = episode if isinstance(episode, list) else [episode]
        try:
            ep_ints = [int(e) for e in ep_list]
        except (TypeError, ValueError):
            skipped.append(v.name)
            skipped_details.append(_skip_detail(v.name, "invalid_episode"))
            continue
        added.append(layout.tv_filename(int(season_num), ep_ints, v.ext))
    return added, skipped, skipped_details


def _pick_query(
    videos: list[RemoteFile],
    hint: Optional[str],
    media_type_override: Optional[str],
) -> QueryPick:
    """hint 优先,文件名兜底。hint 通常是用户补的准确名,分享者改名时必须以 hint 为准。"""
    if hint:
        hp = parse_hint(hint)
        season = hp.season
        if season is None and videos:
            season = Guess.from_text(videos[0].name).season
        mt = media_type_override
        if mt is None:
            if season is not None:
                mt = "tv"
            elif videos:
                gm = Guess.from_text(videos[0].name).media_type
                mt = gm
        # TV 条目年份是 series 首播年。用户写在季度 hint 里的年份常是本季年份
        # (例如 "第五季(2026)"),不能拿它限制 TMDB TV 搜索。
        year = None if mt == "tv" and season is not None else hp.year
        return QueryPick(
            query=hp.title or (videos[0].name if videos else ""),
            year=year,
            season=season,
            media_type=mt,
        )

    if not videos:
        return QueryPick(query="", year=None, season=None, media_type=media_type_override)
    g = Guess.from_text(videos[0].name)
    return QueryPick(
        query=g.title or videos[0].name,
        year=g.year,
        season=g.season,
        media_type=media_type_override or g.media_type,
    )


def _collect_videos_in_parent(
    qc: Cloud, parent_fid: str, new_fids: set[str]
) -> list[tuple[RemoteFile, Optional[int]]]:
    """在父目录里找出 new_fids 对应的条目,递归收集视频。

    返回 (file, parent_season_hint) 列表。parent_season_hint 是从任一祖先
    目录名推断出的 season(S01 / Season 1 / 第一季 等),裸集数文件靠它归位。
    """
    out: list[tuple[RemoteFile, Optional[int]]] = []

    def walk(fid: str, inherited: Optional[int]) -> None:
        for f in qc.list_dir(fid):
            if f.is_dir:
                sub = parse_season_from_name(f.name)
                walk(f.fid, sub if sub is not None else inherited)
            elif f.is_video:
                out.append((f, inherited))

    children = qc.list_dir(parent_fid)
    for c in children:
        if c.fid not in new_fids:
            continue
        if c.is_dir:
            season = parse_season_from_name(c.name)
            walk(c.fid, season)
        elif c.is_video:
            out.append((c, None))
    return out


def _filter_staged_videos_to_share(
    staged: list[tuple[RemoteFile, Optional[int]]],
    share_videos: list[RemoteFile],
) -> list[tuple[RemoteFile, Optional[int]]]:
    """Keep only videos that look like the current share.

    Used for 115 duplicate receives: the API may say the share was already
    received and return no new fids, so we locate the existing top-level item
    in staging by name and then trim it back to this share's video names/sizes.
    """
    names = {f.name for f in share_videos}
    name_sizes = {(f.name, f.size) for f in share_videos if f.size}
    out: list[tuple[RemoteFile, Optional[int]]] = []
    for video, folder_season in staged:
        if name_sizes and (video.name, video.size) in name_sizes:
            out.append((video, folder_season))
        elif video.name in names:
            out.append((video, folder_season))
    return out


def _collect_existing_staging_items_by_name(
    qc: Cloud,
    staging_fid: str,
    top_names: set[str],
    share_videos: list[RemoteFile],
) -> list[tuple[RemoteFile, Optional[int]]]:
    if not top_names:
        return []
    existing_top_fids = {
        f.fid for f in qc.list_dir(staging_fid)
        if f.name in top_names
    }
    if not existing_top_fids:
        return []
    staged = _collect_videos_in_parent(qc, staging_fid, existing_top_fids)
    return _filter_staged_videos_to_share(staged, share_videos)


def ingest(
    cfg: Config,
    url: str,
    hint: Optional[str] = None,
    media_type: Optional[str] = None,
    passcode: Optional[str] = None,
    auto_yes: bool = False,
    dry_run: bool = False,
    tmdb_id: Optional[int] = None,
    season: Optional[int] = None,
    variety: bool = False,
    rename_plan: Any = None,
) -> IngestResult:
    # ---- 1. 按 URL 选云盘 + 解析分享链接 + 拿 stoken ----
    cloud_name, pwd_id, pw_from_url = cloud_parse_share_url(url)
    passcode = passcode or pw_from_url or ""
    qc, _ = cloud_from_url(url, cfg)
    # cloud_name 可能是 "115"(不是合法 Python 属性名),单独映射
    _CLOUD_CFG_ATTR = {"115": "drive115"}
    cloud_cfg = getattr(cfg, _CLOUD_CFG_ATTR.get(cloud_name, cloud_name))
    stoken = qc.get_stoken(pwd_id, passcode)

    # ---- 2. 列分享内容(递归找所有视频) ----
    share_all = qc.list_share_recursive(pwd_id, stoken, "0")
    share_videos = [f for f in share_all if f.is_video]
    if not share_videos:
        return IngestResult(status="error", message="分享里没找到视频文件")
    rename_rules = normalize_rename_plan(rename_plan)
    renamed_rows: list[dict] = []
    if rename_rules:
        virtual_staged, renamed_rows = _apply_rename_rules(
            None,
            [(f, None) for f in share_videos],
            rename_rules,
            dry_run=True,
        )
        share_videos = [f for f, _ in virtual_staged]

    # ---- 3/4. TMDB 识别 ----
    tmdb = TMDB(cfg.tmdb.api_key, cfg.tmdb.language)
    season_hint = season
    query = ""
    chosen_details: Optional[dict[str, Any]] = None

    if tmdb_id is not None:
        mt = media_type or ("tv" if season is not None else None)
        if mt == "movie":
            details = tmdb.movie_details(tmdb_id)
            chosen_details = details
            chosen = TMDBResult(
                id=tmdb_id,
                media_type="movie",
                title=details.get("title") or details.get("original_title") or "",
                original_title=details.get("original_title") or "",
                year=(details.get("release_date") or "").split("-")[0] or None,
                overview=details.get("overview") or "",
                popularity=float(details.get("popularity") or 0.0),
                poster_path=details.get("poster_path"),
            )
        else:
            details = tmdb.tv_details(tmdb_id)
            chosen_details = details
            chosen = TMDBResult(
                id=tmdb_id,
                media_type="tv",
                title=details.get("name") or details.get("original_name") or "",
                original_title=details.get("original_name") or "",
                year=(details.get("first_air_date") or "").split("-")[0] or None,
                overview=details.get("overview") or "",
                popularity=float(details.get("popularity") or 0.0),
                poster_path=details.get("poster_path"),
            )
        if chosen.media_type == "tv" and season_hint is None:
            season_hint = 1
    else:
        pick = _pick_query(share_videos, hint, media_type)
        query = pick.query
        season_hint = season_hint or pick.season
        mt = pick.media_type
        if season_hint is not None and mt is None:
            mt = "tv"
        if not query:
            return IngestResult(status="error", message="无法从文件名或 hint 推断标题")

        candidates = tmdb.search(query, year=pick.year, media_type=mt)[:5]
        if not candidates:
            return IngestResult(
                status="error",
                message=f"TMDB 未找到:{query} (year={pick.year} type={mt})",
            )

        # 未指定年份时,同名候选按年份倒序(离现在最近的优先),非同名按热度保持原序
        if pick.year is None:
            query_lower = query.strip().lower()
            def _sort_key(c: TMDBResult) -> tuple:
                title_match = (
                    c.title.lower() == query_lower
                    or c.original_title.lower() == query_lower
                )
                year_int = int(c.year) if c.year and c.year.isdigit() else 0
                return (not title_match, -year_int, -c.popularity)
            candidates = sorted(candidates, key=_sort_key)

        if (
            not auto_yes
            and len(candidates) > 1
            and cfg.policy.ask_when_ambiguous
        ):
            return IngestResult(
                status="need_confirm",
                candidates=[
                    {
                        "tmdb_id": c.id,
                        "type": c.media_type,
                        "title": c.title,
                        "year": c.year,
                        "popularity": round(c.popularity, 1),
                        "overview": c.overview[:100],
                    }
                    for c in candidates
                ],
            )

        chosen = candidates[0]

    if variety and (chosen.media_type != "tv" or season_hint is None):
        return IngestResult(
            status="error",
            type=chosen.media_type,
            tmdb_id=chosen.id,
            title=chosen.title,
            year=chosen.year,
            message="综艺严格模式需要 TV 条目和明确 season",
        )

    if chosen.media_type == "tv" and chosen_details is None:
        chosen_details = tmdb.tv_details(chosen.id)
    is_variety_show = (
        chosen.media_type == "tv"
        and (variety or _tmdb_says_variety(chosen_details or {}))
    )
    tv_library_root = (
        _tv_library_root(cloud_cfg, is_variety=is_variety_show)
        if chosen.media_type == "tv"
        else cloud_cfg.library_tv
    )

    layout = Layout(title=chosen.title, year=chosen.year, media_type=chosen.media_type)
    variety_matches = []
    if variety and chosen.media_type == "tv" and season_hint is not None:
        season_details = tmdb.tv_season(chosen.id, season_hint)
        episodes = build_variety_episodes(season_details)
        variety_matches = match_variety_files(share_videos, episodes)
        if not variety_matches:
            return IngestResult(
                status="error",
                type="tv",
                tmdb_id=chosen.id,
                title=chosen.title,
                year=chosen.year,
                message=f"综艺严格匹配未找到可入库正片 season={season_hint}",
                skipped=[f.name for f in share_videos[:50]],
                skipped_details=[
                    _skip_detail(f.name, "variety_unmatched", season=season_hint)
                    for f in share_videos[:50]
                ],
                renamed=renamed_rows,
            )

    if dry_run:
        planned = []
        plan_rows = []
        skipped_names: list[str] = []
        skipped_details: list[dict] = []
        if variety_matches and season_hint is not None:
            for m in variety_matches:
                target = layout.tv_filename(season_hint, m.episode.number, m.file.ext)
                planned.append(target)
                plan_rows.append({
                    "episode": m.episode.number,
                    "source": m.file.name,
                    "target": target,
                    "score": m.score,
                    "reasons": list(m.reasons),
                })
            skipped_names = [
                f.name for f in share_videos if f.fid not in {m.file.fid for m in variety_matches}
            ][:50]
            skipped_details = [
                _skip_detail(f.name, "variety_unmatched", season=season_hint)
                for f in share_videos if f.fid not in {m.file.fid for m in variety_matches}
            ][:50]
        elif chosen.media_type == "tv":
            planned, skipped_names, skipped_details = _plan_tv_targets(
                layout, [(f, None) for f in share_videos], season_hint
            )
        elif chosen.media_type == "movie":
            planned = [
                layout.movie_filename(f.ext, part=(i + 1) if len(share_videos) > 1 else None)
                for i, f in enumerate(share_videos)
            ]
        return IngestResult(
            status="ok",
            type=chosen.media_type,
            tmdb_id=chosen.id,
            title=chosen.title,
            year=chosen.year,
            path=(
                layout.movie_dir(cloud_cfg.library_movies)
                if chosen.media_type == "movie"
                else layout.tv_show_dir(tv_library_root)
            ),
            added=planned,
            planned=plan_rows,
            renamed=renamed_rows,
            skipped=skipped_names,
            skipped_details=skipped_details,
            message=(
                f"dry_run — cloud={cloud_name} query='{query}' season={season_hint} "
                f"variety={variety} matched={len(variety_matches)} 未执行转存"
            ),
        )

    # ---- 5. 选定 staging,转存 ----
    if chosen.media_type == "movie":
        staging_path = cloud_cfg.staging_movies
    else:
        staging_path = cloud_cfg.staging_tv
    staging_fid = qc.mkdir_p(staging_path)

    # 普通入库转存分享根;综艺严格模式只转存已匹配到 TMDB 正集的文件。
    top_items = qc.list_share(pwd_id, stoken, "0")
    save_items = [m.file for m in variety_matches] if variety_matches else top_items
    fid_list = [x.fid for x in save_items]
    token_list = [x.fid_token or "" for x in save_items]
    # 源分享里所有视频数(递归),用于确认 copy 完成
    all_share = qc.list_share_recursive(pwd_id, stoken, "0")
    all_share_videos = [f for f in all_share if f.is_video]
    fallback_share_videos = [m.file for m in variety_matches] if variety_matches else all_share_videos
    fallback_top_names = {x.name for x in save_items}
    expected_video_count = (
        len(variety_matches)
        if variety_matches
        else len(all_share_videos)
    )

    # 115 等不返回新 fid 的云盘:先拍快照,copy 后扫新增
    staging_snapshot: set[str] = {f.fid for f in qc.list_dir(staging_fid)}
    saved_top_fids = qc.save_share(pwd_id, stoken, fid_list, token_list, staging_fid)

    # ---- 5.5 确保 staging 目录季号与 hint 一致 ----
    if season_hint is not None and saved_top_fids:
        _ensure_staging_season_match(qc, staging_fid, set(saved_top_fids), season_hint)

    # ---- 6. 收集已转存的视频(轮询等待异步 copy) ----
    staged_videos: list[tuple[RemoteFile, Optional[int]]] = []
    deadline = time.time() + 90
    last_count = -1
    no_progress = 0          # 连续无进展次数,用于退避

    def _poll_sleep(changed: bool) -> None:
        """有进展 → 1.5s;连续无进展时指数退避,最长 10s。"""
        nonlocal no_progress
        if changed:
            no_progress = 0
            time.sleep(1.5)
        else:
            no_progress += 1
            time.sleep(min(1.5 * (2 ** (no_progress - 1)), 10))

    if saved_top_fids:
        # 夸克/阿里:用返回的新 fid 集合精确定位
        saved_set = set(saved_top_fids)
        while True:
            staged_videos = _collect_videos_in_parent(qc, staging_fid, saved_set)
            if len(staged_videos) >= expected_video_count:
                break
            if time.time() >= deadline:
                break
            changed = len(staged_videos) != last_count
            last_count = len(staged_videos)
            _poll_sleep(changed)
    else:
        # 115/百度等:扫 staging 里快照之后新增的顶层条目
        while True:
            staging_children = qc.list_dir(staging_fid)
            new_fids = {f.fid for f in staging_children if f.fid not in staging_snapshot}
            if new_fids:
                staged_videos = _collect_videos_in_parent(qc, staging_fid, new_fids)
            else:
                # 115 对重复接收同一分享会返回“已接收”且没有新 fid。
                # 此时快照差为空,需要复用 staging 中同名的既有顶层条目。
                staged_videos = _collect_existing_staging_items_by_name(
                    qc, staging_fid, fallback_top_names, fallback_share_videos
                )
            if len(staged_videos) >= expected_video_count:
                break
            if time.time() >= deadline:
                break
            changed = len(staged_videos) != last_count
            last_count = len(staged_videos)
            _poll_sleep(changed)

    if not staged_videos:
        return IngestResult(
            status="error",
            message="转存后未在 staging 找到视频",
        )
    if rename_rules:
        staged_videos, renamed_rows = _apply_rename_rules(
            qc, staged_videos, rename_rules, dry_run=False,
        )

    # ---- 7. 落库 ----
    if chosen.media_type == "movie":
        result = _finalize_movie(qc, cfg, cloud_cfg, layout, staged_videos)
    else:
        result = _finalize_tv(
            qc, cfg, cloud_cfg, layout, staged_videos, season_hint,
            tmdb=tmdb if (cfg.policy.write_metadata or variety) else None,
            tmdb_id=chosen.id,
            variety=variety,
            library_tv_root=tv_library_root,
        )
    result.tmdb_id = chosen.id
    result.renamed = renamed_rows

    # ---- 8. 刮削剧/片级元数据(tvshow.nfo / movie.nfo + poster + fanart) ----
    if cfg.policy.write_metadata and result.status == "ok":
        try:
            result.metadata.extend(
                _write_show_metadata(
                    qc, tmdb, cloud_cfg, layout, chosen,
                    library_tv_root=tv_library_root if chosen.media_type == "tv" else None,
                )
            )
        except Exception as e:
            # 元数据写入失败不影响主流程
            if result.message:
                result.message += f" | 元数据失败: {e}"
            else:
                result.message = f"元数据失败: {e}"

    # ---- 9. 清理 staging 留下的空壳 ----
    _cleanup_empty(qc, staging_fid, set(saved_top_fids))
    return result


def ingest_folder(
    cfg: Config,
    cloud_name: str,
    folder_path: str,
    hint: Optional[str] = None,
    media_type: Optional[str] = None,
    auto_yes: bool = False,
    dry_run: bool = False,
    tmdb_id: Optional[int] = None,
    season: Optional[int] = None,
    variety: bool = False,
    rename_plan: Any = None,
) -> IngestResult:
    qc = cloud_by_name(cloud_name, cfg)
    cloud_cfg = _cloud_config(cfg, cloud_name)
    folder_fid = qc.resolve_path(folder_path)
    if folder_fid is None:
        return IngestResult(status="error", message=f"找不到网盘目录:{folder_path}")

    original_staged_videos = _collect_videos_from_drive_folder(qc, folder_fid, folder_path)
    if not original_staged_videos:
        return IngestResult(status="error", message=f"目录里没找到视频文件:{folder_path}")

    staged_videos = original_staged_videos
    rename_rules = normalize_rename_plan(rename_plan)
    renamed_rows: list[dict] = []
    if rename_rules:
        staged_videos, renamed_rows = _apply_rename_rules(
            None,
            staged_videos,
            rename_rules,
            dry_run=True,
        )
    videos = [v for v, _ in staged_videos]

    tmdb = TMDB(cfg.tmdb.api_key, cfg.tmdb.language)
    season_hint = season
    query = ""
    chosen_details: Optional[dict[str, Any]] = None

    if tmdb_id is not None:
        mt = media_type or ("tv" if season is not None else None)
        if mt == "movie":
            details = tmdb.movie_details(tmdb_id)
            chosen_details = details
            chosen = TMDBResult(
                id=tmdb_id,
                media_type="movie",
                title=details.get("title") or details.get("original_title") or "",
                original_title=details.get("original_title") or "",
                year=(details.get("release_date") or "").split("-")[0] or None,
                overview=details.get("overview") or "",
                popularity=float(details.get("popularity") or 0.0),
                poster_path=details.get("poster_path"),
            )
        else:
            details = tmdb.tv_details(tmdb_id)
            chosen_details = details
            chosen = TMDBResult(
                id=tmdb_id,
                media_type="tv",
                title=details.get("name") or details.get("original_name") or "",
                original_title=details.get("original_name") or "",
                year=(details.get("first_air_date") or "").split("-")[0] or None,
                overview=details.get("overview") or "",
                popularity=float(details.get("popularity") or 0.0),
                poster_path=details.get("poster_path"),
            )
        if chosen.media_type == "tv" and season_hint is None:
            season_hint = 1
    else:
        pick = _pick_query(videos, hint, media_type)
        query = pick.query
        season_hint = season_hint or pick.season
        mt = pick.media_type
        if season_hint is not None and mt is None:
            mt = "tv"
        if not query:
            return IngestResult(
                status="error",
                message="无法从文件名或 hint 推断标题",
                renamed=renamed_rows,
            )
        candidates = tmdb.search(query, year=pick.year, media_type=mt)[:5]
        if not candidates:
            return IngestResult(
                status="error",
                message=f"TMDB 未找到:{query} (year={pick.year} type={mt})",
                renamed=renamed_rows,
            )
        if pick.year is None:
            query_lower = query.strip().lower()
            def _sort_key(c: TMDBResult) -> tuple:
                title_match = (
                    c.title.lower() == query_lower
                    or c.original_title.lower() == query_lower
                )
                year_int = int(c.year) if c.year and c.year.isdigit() else 0
                return (not title_match, -year_int, -c.popularity)
            candidates = sorted(candidates, key=_sort_key)
        if (
            not auto_yes
            and len(candidates) > 1
            and cfg.policy.ask_when_ambiguous
        ):
            return IngestResult(
                status="need_confirm",
                renamed=renamed_rows,
                candidates=[
                    {
                        "tmdb_id": c.id,
                        "type": c.media_type,
                        "title": c.title,
                        "year": c.year,
                        "popularity": round(c.popularity, 1),
                        "overview": c.overview[:100],
                    }
                    for c in candidates
                ],
            )
        chosen = candidates[0]

    if variety and (chosen.media_type != "tv" or season_hint is None):
        return IngestResult(
            status="error",
            type=chosen.media_type,
            tmdb_id=chosen.id,
            title=chosen.title,
            year=chosen.year,
            renamed=renamed_rows,
            message="综艺严格模式需要 TV 条目和明确 season",
        )

    if chosen.media_type == "tv" and chosen_details is None:
        chosen_details = tmdb.tv_details(chosen.id)
    is_variety_show = (
        chosen.media_type == "tv"
        and (variety or _tmdb_says_variety(chosen_details or {}))
    )
    tv_library_root = (
        _tv_library_root(cloud_cfg, is_variety=is_variety_show)
        if chosen.media_type == "tv"
        else cloud_cfg.library_tv
    )
    layout = Layout(title=chosen.title, year=chosen.year, media_type=chosen.media_type)

    variety_matches = []
    if variety and chosen.media_type == "tv" and season_hint is not None:
        season_details = tmdb.tv_season(chosen.id, season_hint)
        episodes = build_variety_episodes(season_details)
        variety_matches = match_variety_files(videos, episodes)
        if not variety_matches:
            return IngestResult(
                status="error",
                type="tv",
                tmdb_id=chosen.id,
                title=chosen.title,
                year=chosen.year,
                renamed=renamed_rows,
                message=f"综艺严格匹配未找到可入库正片 season={season_hint}",
                skipped=[v.name for v in videos[:50]],
                skipped_details=[
                    _skip_detail(v.name, "variety_unmatched", season=season_hint)
                    for v in videos[:50]
                ],
            )

    if dry_run:
        planned: list[dict] = []
        added: list[str] = []
        skipped: list[str] = []
        skipped_details: list[dict] = []
        if chosen.media_type == "movie":
            added = [
                layout.movie_filename(v.ext, part=(i + 1) if len(videos) > 1 else None)
                for i, v in enumerate(videos)
            ]
        elif variety_matches and season_hint is not None:
            for m in variety_matches:
                target = layout.tv_filename(season_hint, m.episode.number, m.file.ext)
                added.append(target)
                planned.append({
                    "episode": m.episode.number,
                    "source": m.file.name,
                    "target": target,
                    "score": m.score,
                    "reasons": list(m.reasons),
                })
            matched_fids = {m.file.fid for m in variety_matches}
            skipped = [v.name for v in videos if v.fid not in matched_fids][:50]
            skipped_details = [
                _skip_detail(v.name, "variety_unmatched", season=season_hint)
                for v in videos if v.fid not in matched_fids
            ][:50]
        else:
            added, skipped, skipped_details = _plan_tv_targets(
                layout, staged_videos, season_hint
            )

        return IngestResult(
            status="ok",
            type=chosen.media_type,
            tmdb_id=chosen.id,
            title=chosen.title,
            year=chosen.year,
            path=(
                layout.movie_dir(cloud_cfg.library_movies)
                if chosen.media_type == "movie"
                else layout.tv_show_dir(tv_library_root)
            ),
            added=added,
            skipped=skipped,
            skipped_details=skipped_details,
            planned=planned,
            renamed=renamed_rows,
            message=(
                f"dry_run — cloud={cloud_name} folder='{folder_path}' "
                f"season={season_hint} variety={variety} 未执行移动"
            ),
        )

    if chosen.media_type == "movie":
        result = _finalize_movie(qc, cfg, cloud_cfg, layout, staged_videos)
    else:
        result = _finalize_tv(
            qc, cfg, cloud_cfg, layout, staged_videos, season_hint,
            tmdb=tmdb if (cfg.policy.write_metadata or variety) else None,
            tmdb_id=chosen.id,
            variety=variety,
            library_tv_root=tv_library_root,
        )
    result.tmdb_id = chosen.id
    result.renamed = renamed_rows

    if cfg.policy.write_metadata and result.status == "ok":
        try:
            result.metadata.extend(
                _write_show_metadata(
                    qc, tmdb, cloud_cfg, layout, chosen,
                    library_tv_root=tv_library_root if chosen.media_type == "tv" else None,
                )
            )
        except Exception as e:
            result.message = f"{result.message} | 元数据失败: {e}" if result.message else f"元数据失败: {e}"
    return result


def scrape_folder(
    cfg: Config,
    cloud_name: str,
    folder_path: str,
    hint: Optional[str] = None,
    media_type: Optional[str] = None,
    auto_yes: bool = False,
    dry_run: bool = False,
    tmdb_id: Optional[int] = None,
    season: Optional[int] = None,
    variety: bool = False,
    rename_plan: Any = None,
    force: bool = False,
) -> IngestResult:
    """对网盘内已存在目录原地补刮削元数据,不移动视频文件。"""
    qc = cloud_by_name(cloud_name, cfg)
    folder_fid = qc.resolve_path(folder_path)
    if folder_fid is None:
        return IngestResult(status="error", message=f"找不到网盘目录:{folder_path}")

    scrape_videos = _collect_scrape_videos(qc, folder_fid, folder_path)
    if not scrape_videos:
        return IngestResult(status="error", message=f"目录里没找到视频文件:{folder_path}")

    staged = [(sv.file, sv.folder_season) for sv in scrape_videos]
    rename_rules = normalize_rename_plan(rename_plan)
    renamed_rows: list[dict] = []
    if rename_rules:
        staged, renamed_rows = _apply_rename_rules(
            None if dry_run else qc,
            staged,
            rename_rules,
            dry_run=dry_run,
        )
        scrape_videos = [
            ScrapeVideo(
                file=file,
                folder_season=folder_season,
                parent_fid=sv.parent_fid,
                parent_path=sv.parent_path,
            )
            for sv, (file, folder_season) in zip(scrape_videos, staged)
        ]

    videos = [sv.file for sv in scrape_videos]
    folder_seasons = {sv.folder_season for sv in scrape_videos if sv.folder_season is not None}
    season_hint = season
    if season_hint is None and len(folder_seasons) == 1:
        season_hint = next(iter(folder_seasons))

    tmdb = TMDB(cfg.tmdb.api_key, cfg.tmdb.language)
    chosen_details: Optional[dict[str, Any]] = None
    query = ""

    if tmdb_id is not None:
        mt = media_type or ("tv" if season_hint is not None else None)
        chosen, chosen_details = _tmdb_result_from_details(tmdb, tmdb_id, mt)
        if chosen.media_type == "tv" and season_hint is None:
            season_hint = 1
    else:
        pick = _pick_query(videos, hint, media_type)
        query = pick.query
        season_hint = season_hint or pick.season
        mt = pick.media_type
        if season_hint is not None and mt is None:
            mt = "tv"
        if not query:
            return IngestResult(
                status="error",
                renamed=renamed_rows,
                message="无法从文件名或 hint 推断标题",
            )
        candidates = tmdb.search(query, year=pick.year, media_type=mt)[:5]
        if not candidates:
            return IngestResult(
                status="error",
                renamed=renamed_rows,
                message=f"TMDB 未找到:{query} (year={pick.year} type={mt})",
            )
        if pick.year is None:
            query_lower = query.strip().lower()

            def _sort_key(c: TMDBResult) -> tuple:
                title_match = (
                    c.title.lower() == query_lower
                    or c.original_title.lower() == query_lower
                )
                year_int = int(c.year) if c.year and c.year.isdigit() else 0
                return (not title_match, -year_int, -c.popularity)

            candidates = sorted(candidates, key=_sort_key)
        if (
            not auto_yes
            and len(candidates) > 1
            and cfg.policy.ask_when_ambiguous
        ):
            return IngestResult(
                status="need_confirm",
                renamed=renamed_rows,
                candidates=[
                    {
                        "tmdb_id": c.id,
                        "type": c.media_type,
                        "title": c.title,
                        "year": c.year,
                        "popularity": round(c.popularity, 1),
                        "overview": c.overview[:100],
                    }
                    for c in candidates
                ],
            )
        chosen = candidates[0]

    if variety and (chosen.media_type != "tv" or season_hint is None):
        return IngestResult(
            status="error",
            type=chosen.media_type,
            tmdb_id=chosen.id,
            title=chosen.title,
            year=chosen.year,
            renamed=renamed_rows,
            message="综艺严格模式需要 TV 条目和明确 season",
        )

    layout = Layout(title=chosen.title, year=chosen.year, media_type=chosen.media_type)
    metadata_rows: list[dict] = []
    skipped: list[str] = []
    skipped_details: list[dict] = []

    root_path = folder_path.rstrip("/") or "/"
    if chosen.media_type == "movie":
        movie_nfo_name = f"{layout.folder_name}.nfo"
        if len(videos) == 1:
            movie_nfo_name = f"{videos[0].name.rsplit('.', 1)[0]}.nfo"
        metadata_rows.extend(
            _write_root_metadata(
                qc,
                tmdb,
                folder_fid,
                root_path,
                "movie",
                chosen.id,
                movie_nfo_name,
                details=chosen_details,
                dry_run=dry_run,
                force=force,
            )
        )
        return IngestResult(
            status="ok",
            type="movie",
            tmdb_id=chosen.id,
            title=chosen.title,
            year=chosen.year,
            path=root_path,
            skipped=skipped,
            skipped_details=skipped_details,
            renamed=renamed_rows,
            metadata=metadata_rows,
            message=f"{'dry_run — ' if dry_run else ''}metadata-only cloud={cloud_name} folder='{folder_path}'",
        )

    if chosen_details is None:
        chosen_details = tmdb.tv_details(chosen.id)

    show_root_fid = folder_fid
    show_root_path = root_path
    if parse_season_from_name(root_path.rsplit("/", 1)[-1]) is not None:
        parent_path = _parent_cloud_path(root_path)
        parent_fid = qc.resolve_path(parent_path)
        if parent_fid is not None:
            show_root_fid = parent_fid
            show_root_path = parent_path

    metadata_rows.extend(
        _write_root_metadata(
            qc,
            tmdb,
            show_root_fid,
            show_root_path,
            "tv",
            chosen.id,
            "tvshow.nfo",
            details=chosen_details,
            dry_run=dry_run,
            force=force,
        )
    )

    has_multi_folder_season = len(folder_seasons) > 1
    variety_by_fid: dict[str, int] = {}
    if variety and season_hint is not None:
        try:
            season_details = tmdb.tv_season(chosen.id, season_hint)
            episodes = build_variety_episodes(season_details)
            matches = match_variety_files(videos, episodes)
            variety_by_fid = {m.file.fid: m.episode.number for m in matches}
        except Exception:
            variety_by_fid = {}

    existing_cache: dict[str, dict[str, RemoteFile]] = {}
    for sv in scrape_videos:
        if variety:
            if season_hint is None or sv.file.fid not in variety_by_fid:
                skipped.append(sv.file.name)
                skipped_details.append(
                    _skip_detail(sv.file.name, "variety_unmatched", season=season_hint)
                )
                continue
            s = int(season_hint)
            ep: Any = variety_by_fid[sv.file.fid]
        else:
            g = Guess.from_text(sv.file.name)
            s = g.season
            if s is None:
                s = sv.folder_season
            if s is None:
                s = season_hint
            ep = g.episode
            if ep is None:
                skipped.append(sv.file.name)
                skipped_details.append(
                    _skip_detail(
                        sv.file.name,
                        "unparsed_episode",
                        season=season_hint if s is None else s,
                        folder_season=sv.folder_season,
                    )
                )
                continue
            if s is None:
                if has_multi_folder_season:
                    skipped.append(sv.file.name)
                    skipped_details.append(_skip_detail(sv.file.name, "missing_season"))
                    continue
                s = 1

        ep_list = ep if isinstance(ep, list) else [ep]
        try:
            ep_ints = [int(e) for e in ep_list]
        except (TypeError, ValueError):
            skipped.append(sv.file.name)
            skipped_details.append(_skip_detail(sv.file.name, "invalid_episode", season=s))
            continue

        existing = existing_cache.get(sv.parent_fid)
        if existing is None:
            existing = {f.name: f for f in qc.list_dir(sv.parent_fid)}
            existing_cache[sv.parent_fid] = existing
        metadata_rows.extend(
            _write_episode_metadata(
                qc,
                tmdb,
                chosen.id,
                int(s),
                ep_ints,
                sv.parent_fid,
                sv.file.name,
                existing,
                parent_path=sv.parent_path,
                dry_run=dry_run,
                force=force,
            )
        )

    return IngestResult(
        status="ok",
        type="tv",
        tmdb_id=chosen.id,
        title=chosen.title,
        year=chosen.year,
        path=show_root_path,
        skipped=skipped,
        skipped_details=skipped_details,
        renamed=renamed_rows,
        metadata=metadata_rows,
        message=f"{'dry_run — ' if dry_run else ''}metadata-only cloud={cloud_name} folder='{folder_path}' season={season_hint} variety={variety}",
    )


def _metadata_path(parent_path: str, name: str) -> str:
    return _join_cloud_path(parent_path, name)


def _upload_metadata_file(
    qc: Cloud,
    parent_fid: str,
    parent_path: str,
    name: str,
    kind: str,
    existing: dict[str, RemoteFile],
    payload: Callable[[], bytes],
    *,
    mime: str,
    dry_run: bool = False,
    force: bool = False,
) -> dict:
    existing_file = existing.get(name)
    row = {
        "kind": kind,
        "name": name,
        "path": _metadata_path(parent_path, name),
        "status": "exists",
    }
    if existing_file is not None and not force:
        return row
    if dry_run:
        row["status"] = "would_overwrite" if existing_file is not None else "would_create"
        return row

    try:
        if existing_file is not None and force:
            qc.delete([existing_file.fid])
        new_fid = qc.upload_bytes(parent_fid, name, payload(), mime=mime)
        fallback_fid = existing_file.fid if existing_file is not None else name
        existing[name] = RemoteFile(
            fid=new_fid or fallback_fid,
            name=name,
            is_dir=False,
            parent_fid=parent_fid,
        )
        row["status"] = "overwritten" if existing_file is not None else "created"
    except Exception as e:
        row["status"] = "error"
        row["message"] = str(e)
    return row


def _write_root_metadata(
    qc: Cloud,
    tmdb: TMDB,
    target_fid: str,
    target_path: str,
    media_type: str,
    tmdb_id: int,
    nfo_name: str,
    *,
    details: Optional[dict[str, Any]] = None,
    dry_run: bool = False,
    force: bool = False,
) -> list[dict]:
    if media_type == "movie":
        details = details or tmdb.movie_details(tmdb_id)
        nfo_text = nfo_mod.movie_nfo(details)
    else:
        details = details or tmdb.tv_details(tmdb_id)
        nfo_text = nfo_mod.tvshow_nfo(details)

    existing = {f.name: f for f in qc.list_dir(target_fid)}
    rows = [
        _upload_metadata_file(
            qc,
            target_fid,
            target_path,
            nfo_name,
            "movie_nfo" if media_type == "movie" else "tvshow_nfo",
            existing,
            lambda text=nfo_text: text.encode("utf-8"),
            mime="application/xml",
            dry_run=dry_run,
            force=force,
        )
    ]

    poster_url = artwork.build_url(details.get("poster_path"))
    if poster_url:
        rows.append(
            _upload_metadata_file(
                qc,
                target_fid,
                target_path,
                "poster.jpg",
                "poster",
                existing,
                lambda url=poster_url: artwork.download(url),
                mime="image/jpeg",
                dry_run=dry_run,
                force=force,
            )
        )

    fanart_url = artwork.build_url(details.get("backdrop_path"))
    if fanart_url:
        rows.append(
            _upload_metadata_file(
                qc,
                target_fid,
                target_path,
                "fanart.jpg",
                "fanart",
                existing,
                lambda url=fanart_url: artwork.download(url),
                mime="image/jpeg",
                dry_run=dry_run,
                force=force,
            )
        )
    return rows


def _write_show_metadata(
    qc: Cloud,
    tmdb: TMDB,
    cloud_cfg: Any,
    layout: Layout,
    chosen: TMDBResult,
    library_tv_root: Optional[str] = None,
) -> list[dict]:
    """拉 TMDB 详情 → 生成 tvshow.nfo / movie.nfo → 下载 poster/fanart → 上传到媒体库根目录。"""
    if chosen.media_type == "movie":
        target_dir = layout.movie_dir(cloud_cfg.library_movies)
        nfo_name = f"{layout.folder_name}.nfo"
    else:
        target_dir = layout.tv_show_dir(library_tv_root or cloud_cfg.library_tv)
        nfo_name = "tvshow.nfo"

    target_fid = qc.mkdir_p(target_dir)
    return _write_root_metadata(
        qc,
        tmdb,
        target_fid,
        target_dir,
        chosen.media_type,
        chosen.id,
        nfo_name,
    )


def _write_episode_metadata(
    qc: Cloud,
    tmdb: TMDB,
    tmdb_id: int,
    season: int,
    ep_ints: list[int],
    season_fid: str,
    video_filename: str,
    existing: dict[str, RemoteFile],
    *,
    parent_path: str,
    dry_run: bool = False,
    force: bool = False,
) -> list[dict]:
    """为单集视频写 {base}.nfo + {base}-thumb.jpg。

    video_filename 形如 "标题 - S01E02.mp4",base 即去掉扩展名。
    多集合并(S01E01-E02)只取第一集的 TMDB 详情。
    """
    base = video_filename.rsplit(".", 1)[0]
    nfo_name = f"{base}.nfo"
    thumb_name = f"{base}-thumb.jpg"

    if nfo_name in existing and thumb_name in existing and not force:
        return [
            {
                "kind": "episode_nfo",
                "name": nfo_name,
                "path": _metadata_path(parent_path, nfo_name),
                "status": "exists",
            },
            {
                "kind": "episode_thumb",
                "name": thumb_name,
                "path": _metadata_path(parent_path, thumb_name),
                "status": "exists",
            },
        ]

    try:
        ep_detail = tmdb.tv_episode(tmdb_id, season, ep_ints[0])
    except Exception as e:
        return [
            {
                "kind": "episode_metadata",
                "name": base,
                "path": _metadata_path(parent_path, video_filename),
                "status": "error",
                "message": str(e),
            }
        ]

    rows = [
        _upload_metadata_file(
            qc,
            season_fid,
            parent_path,
            nfo_name,
            "episode_nfo",
            existing,
            lambda detail=ep_detail: nfo_mod.episode_nfo(detail).encode("utf-8"),
            mime="application/xml",
            dry_run=dry_run,
            force=force,
        )
    ]

    still = ep_detail.get("still_path")
    if still:
        url = artwork.build_url(still)
        if url:
            rows.append(
                _upload_metadata_file(
                    qc,
                    season_fid,
                    parent_path,
                    thumb_name,
                    "episode_thumb",
                    existing,
                    lambda thumb_url=url: artwork.download(thumb_url),
                    mime="image/jpeg",
                    dry_run=dry_run,
                    force=force,
                )
            )
    return rows


def _cleanup_empty(qc: Cloud, staging_fid: str, top_fids: set[str]) -> None:
    """删掉 staging 里本次转存顶层条目中已空的文件夹。只动本次产物。

    对 115/百度等 save_share 不返回新 fid 的云盘,top_fids 为空,此时清理
    staging 下所有空文件夹(中转站通常安全)。
    """
    try:
        children = qc.list_dir(staging_fid)
    except Exception:
        return
    to_delete: list[str] = []
    for c in children:
        if not c.is_dir:
            continue
        if top_fids and c.fid not in top_fids:
            continue
        remaining = qc.list_dir_recursive(c.fid)
        if not any(f.is_video for f in remaining):
            to_delete.append(c.fid)
    if to_delete:
        try:
            qc.delete(to_delete)
        except Exception:
            pass


def _ensure_staging_season_match(
    qc: Cloud,
    staging_fid: str,
    top_fids: set[str],
    season_hint: int,
) -> None:
    """转存后检查 staging 目录季号是否与 hint 一致。

    逻辑:
    - 目录季号正确(= hint) → 不动
    - 目录季号错误(≠ hint) → 改成正确季号
    - 目录无季号 → 加上正确季号

    只处理本次转存的顶层目录(top_fids),不动 staging 里其他已有内容。
    注意:仅适用于 save_share 返回 fid 列表的云盘(夸克/阿里);
    115/百度返回空列表,此步骤跳过,为已知限制。
    """
    try:
        children = qc.list_dir(staging_fid)
    except Exception:
        return

    for c in children:
        if not c.is_dir:
            continue
        if c.fid not in top_fids:
            continue

        parsed = parse_season_from_name(c.name)

        if parsed is not None:
            if parsed == season_hint:
                continue  # 正确,无需处理
            # 季号错误:替换为正确季号
            new_name = re.sub(
                r'第\s*[0-9零〇一二两三四五六七八九十]+\s*季',
                f'第{season_hint}季',
                c.name,
            )
            new_name = re.sub(r'[Ss]eason[\s._\-]*\d+', f'Season {season_hint}', new_name)
            new_name = re.sub(r'\b[sS]\d{1,2}\b', f'S{season_hint:02d}', new_name)
            if new_name == c.name:  # 正则没匹配到,直接加后缀
                new_name = f"{c.name} 第{season_hint}季"
        else:
            # 无季号:加上正确季号
            new_name = f"{c.name} 第{season_hint}季"

        try:
            qc.rename(c.fid, new_name)
        except Exception:
            pass  # 重命名失败不影响主流程


def _finalize_movie(
    qc: Cloud,
    cfg: Config,
    cloud_cfg: Any,
    layout: Layout,
    staged: list[tuple[RemoteFile, Optional[int]]],
) -> IngestResult:
    videos: list[RemoteFile] = [v for v, _ in staged]
    target_dir = layout.movie_dir(cloud_cfg.library_movies)
    target_fid = qc.mkdir_p(target_dir)
    existing = qc.list_dir(target_fid)
    already_has_video = any(f.is_video for f in existing)

    if already_has_video and cfg.policy.on_movie_exists == "skip":
        # 把 staged 挪去 rejected
        if cfg.policy.rejected_dir_movies:
            rej_fid = qc.mkdir_p(cfg.policy.rejected_dir_movies)
            qc.move([v.fid for v in videos], rej_fid)
        skipped_details = [
            _skip_detail(
                v.name,
                "movie_exists",
                target_dir=target_dir,
                action="moved_to_rejected" if cfg.policy.rejected_dir_movies else "skipped",
            )
            for v in videos
        ]
        return IngestResult(
            status="skipped",
            type="movie",
            title=layout.title,
            year=layout.year,
            path=target_dir,
            skipped=[v.name for v in videos],
            skipped_details=skipped_details,
            message="库里已有,按策略跳过",
        )

    added_names: list[str] = []
    for i, v in enumerate(videos):
        new_name = layout.movie_filename(
            v.ext, part=(i + 1) if len(videos) > 1 else None
        )
        if new_name != v.name:
            qc.rename(v.fid, new_name)
        added_names.append(new_name)
    qc.move([v.fid for v in videos], target_fid)

    return IngestResult(
        status="ok",
        type="movie",
        title=layout.title,
        year=layout.year,
        path=target_dir,
        added=added_names,
    )


def _finalize_tv(
    qc: Cloud,
    cfg: Config,
    cloud_cfg: Any,
    layout: Layout,
    staged: list[tuple[RemoteFile, Optional[int]]],
    season_hint: Optional[int],
    tmdb: Optional[TMDB] = None,
    tmdb_id: Optional[int] = None,
    variety: bool = False,
    library_tv_root: Optional[str] = None,
) -> IngestResult:
    # 多季分享(父目录明示了 >=2 个不同 season)里,裸集数不再默认到 S01
    has_multi_folder_season = len({fs for _, fs in staged if fs is not None}) > 1

    # 按 (season, episode) 解析每个 staged 视频
    parsed: list[tuple[int, Any, RemoteFile]] = []  # season, episode(int|list), file
    skipped_details: list[dict] = []
    variety_by_fid: dict[str, int] = {}
    if variety and tmdb is not None and tmdb_id is not None and season_hint is not None:
        try:
            season_details = tmdb.tv_season(tmdb_id, season_hint)
            episodes = build_variety_episodes(season_details)
            matches = match_variety_files([v for v, _ in staged], episodes)
            variety_by_fid = {m.file.fid: m.episode.number for m in matches}
        except Exception:
            variety_by_fid = {}

    for v, folder_season in staged:
        if variety:
            if season_hint is None or v.fid not in variety_by_fid:
                skipped_details.append(
                    _skip_detail(v.name, "variety_unmatched", season=season_hint)
                )
                continue
            parsed.append((int(season_hint), variety_by_fid[v.fid], v))
            continue
        g = Guess.from_text(v.name)
        # 优先级: 文件名 SxxExx > 父目录 season > hint > 单季默认 1
        s = g.season
        if s is None:
            s = folder_season
        if s is None:
            s = season_hint
        ep = g.episode
        if ep is None:
            skipped_details.append(
                _skip_detail(
                    v.name,
                    "unparsed_episode",
                    season=season_hint if s is None else s,
                    folder_season=folder_season,
                )
            )
            continue
        if s is None:
            # 多季分享里裸集数归不到 season 就算孤儿,不再默认 1
            if has_multi_folder_season:
                skipped_details.append(_skip_detail(v.name, "missing_season"))
                continue
            s = 1
        parsed.append((int(s), ep, v))

    if not parsed:
        return IngestResult(
            status="error",
            type="tv",
            title=layout.title,
            year=layout.year,
            message="无法从文件名解析 SxxExx",
            skipped=[row["name"] for row in skipped_details],
            skipped_details=skipped_details,
        )

    added: list[str] = []
    skipped: list[str] = [row["name"] for row in skipped_details]
    metadata_rows: list[dict] = []
    seasons = sorted({s for s, _, _ in parsed})
    last_target: str = ""

    # 剧目录:先解析/创建一次,后面复用里面的季子目录
    tv_root = library_tv_root or cloud_cfg.library_tv
    show_dir = layout.tv_show_dir(tv_root)
    show_fid = qc.mkdir_p(show_dir)
    show_children = qc.list_dir(show_fid)

    for s in seasons:
        # 先尝试复用现有任意命名的 season 目录(Season 1 / S01 / 第一季 等)
        existing_season = find_season_folder(show_children, s)
        if existing_season is not None:
            season_fid = existing_season.fid
            season_dir = f"{show_dir}/{existing_season.name}"
        else:
            season_dir = layout.season_dir(tv_root, s)
            season_fid = qc.mkdir_p(season_dir)
            # 刷新 show_children,后续 season 也能看到本轮新建的目录
            show_children = qc.list_dir(show_fid)
        last_target = season_dir
        existing_files = qc.list_dir(season_fid)
        existing_eps = scan_existing_episodes(existing_files, s)
        existing_by_name = {f.name: f for f in existing_files}

        for ss, ep, v in parsed:
            if ss != s:
                continue
            ep_list = ep if isinstance(ep, list) else [ep]
            try:
                ep_ints = [int(e) for e in ep_list]
            except (TypeError, ValueError):
                skipped.append(v.name)
                skipped_details.append(
                    _skip_detail(v.name, "invalid_episode", season=s)
                )
                continue

            if any(e in existing_eps for e in ep_ints):
                skipped.append(v.name)
                target_name = layout.tv_filename(s, ep_ints, v.ext)
                action = "moved_to_rejected" if cfg.policy.rejected_dir_tv else "skipped"
                skipped_details.append(
                    _skip_detail(
                        v.name,
                        "existing_episode",
                        season=s,
                        episodes=ep_ints,
                        target=target_name,
                        target_dir=season_dir,
                        action=action,
                    )
                )
                if cfg.policy.rejected_dir_tv:
                    rej_fid = qc.mkdir_p(cfg.policy.rejected_dir_tv)
                    qc.move([v.fid], rej_fid)
                continue

            new_name = layout.tv_filename(s, ep_ints, v.ext)
            if new_name != v.name:
                qc.rename(v.fid, new_name)
            qc.move([v.fid], season_fid)
            added.append(new_name)
            existing_eps.update(ep_ints)
            existing_by_name[new_name] = replace(v, name=new_name, parent_fid=season_fid)

            # 每集 NFO + thumb
            if cfg.policy.write_metadata and tmdb is not None and tmdb_id is not None:
                try:
                    metadata_rows.extend(
                        _write_episode_metadata(
                            qc, tmdb, tmdb_id, s, ep_ints,
                            season_fid, new_name, existing_by_name,
                            parent_path=season_dir,
                        )
                    )
                except Exception:
                    pass

    return IngestResult(
        status="ok",
        type="tv",
        title=layout.title,
        year=layout.year,
        path=last_target or layout.tv_show_dir(tv_root),
        added=added,
        skipped=skipped,
        skipped_details=skipped_details,
        metadata=metadata_rows,
    )
