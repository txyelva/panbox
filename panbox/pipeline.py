from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .clouds import from_url as cloud_from_url
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
    candidates: list[dict] = field(default_factory=list)
    planned: list[dict] = field(default_factory=list)
    message: Optional[str] = None


@dataclass
class QueryPick:
    query: str
    year: Optional[int]
    season: Optional[int]
    media_type: Optional[str]


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
        return QueryPick(
            query=hp.title or (videos[0].name if videos else ""),
            year=hp.year,
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
            )

    if dry_run:
        planned = []
        plan_rows = []
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
            skipped=[] if not variety_matches else [
                f.name for f in share_videos if f.fid not in {m.file.fid for m in variety_matches}
            ][:50],
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
    expected_video_count = (
        len(variety_matches)
        if variety_matches
        else sum(1 for f in all_share if f.is_video)
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
            new_fids = {
                f.fid for f in qc.list_dir(staging_fid)
                if f.fid not in staging_snapshot
            }
            if new_fids:
                staged_videos = _collect_videos_in_parent(qc, staging_fid, new_fids)
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

    # ---- 8. 刮削剧/片级元数据(tvshow.nfo / movie.nfo + poster + fanart) ----
    if cfg.policy.write_metadata and result.status == "ok":
        try:
            _write_show_metadata(
                qc, tmdb, cloud_cfg, layout, chosen,
                library_tv_root=tv_library_root if chosen.media_type == "tv" else None,
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


def _write_show_metadata(
    qc: Cloud,
    tmdb: TMDB,
    cloud_cfg: Any,
    layout: Layout,
    chosen: TMDBResult,
    library_tv_root: Optional[str] = None,
) -> None:
    """拉 TMDB 详情 → 生成 tvshow.nfo / movie.nfo → 下载 poster/fanart → 上传到媒体库根目录。"""
    if chosen.media_type == "movie":
        details = tmdb.movie_details(chosen.id)
        target_dir = layout.movie_dir(cloud_cfg.library_movies)
        nfo_name = f"{layout.folder_name}.nfo"
        nfo_text = nfo_mod.movie_nfo(details)
    else:
        details = tmdb.tv_details(chosen.id)
        target_dir = layout.tv_show_dir(library_tv_root or cloud_cfg.library_tv)
        nfo_name = "tvshow.nfo"
        nfo_text = nfo_mod.tvshow_nfo(details)

    target_fid = qc.mkdir_p(target_dir)
    existing = {f.name for f in qc.list_dir(target_fid)}

    if nfo_name not in existing:
        qc.upload_bytes(
            target_fid,
            nfo_name,
            nfo_text.encode("utf-8"),
            mime="application/xml",
        )

    poster_url = artwork.build_url(details.get("poster_path"))
    if poster_url and "poster.jpg" not in existing:
        try:
            data = artwork.download(poster_url)
            qc.upload_bytes(target_fid, "poster.jpg", data, mime="image/jpeg")
        except Exception:
            pass

    fanart_url = artwork.build_url(details.get("backdrop_path"))
    if fanart_url and "fanart.jpg" not in existing:
        try:
            data = artwork.download(fanart_url)
            qc.upload_bytes(target_fid, "fanart.jpg", data, mime="image/jpeg")
        except Exception:
            pass


def _write_episode_metadata(
    qc: Cloud,
    tmdb: TMDB,
    tmdb_id: int,
    season: int,
    ep_ints: list[int],
    season_fid: str,
    video_filename: str,
    existing_names: set[str],
) -> None:
    """为单集视频写 {base}.nfo + {base}-thumb.jpg。

    video_filename 形如 "标题 - S01E02.mp4",base 即去掉扩展名。
    多集合并(S01E01-E02)只取第一集的 TMDB 详情。
    """
    base = video_filename.rsplit(".", 1)[0]
    nfo_name = f"{base}.nfo"
    thumb_name = f"{base}-thumb.jpg"

    if nfo_name in existing_names and thumb_name in existing_names:
        return

    try:
        ep_detail = tmdb.tv_episode(tmdb_id, season, ep_ints[0])
    except Exception:
        return

    if nfo_name not in existing_names:
        try:
            text = nfo_mod.episode_nfo(ep_detail)
            qc.upload_bytes(
                season_fid, nfo_name, text.encode("utf-8"), mime="application/xml"
            )
            existing_names.add(nfo_name)
        except Exception:
            pass

    still = ep_detail.get("still_path")
    if still and thumb_name not in existing_names:
        url = artwork.build_url(still)
        if url:
            try:
                data = artwork.download(url)
                qc.upload_bytes(season_fid, thumb_name, data, mime="image/jpeg")
                existing_names.add(thumb_name)
            except Exception:
                pass


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
        return IngestResult(
            status="skipped",
            type="movie",
            title=layout.title,
            year=layout.year,
            path=target_dir,
            skipped=[v.name for v in videos],
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
    orphans: list[RemoteFile] = []
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
                orphans.append(v)
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
            orphans.append(v)
            continue
        if s is None:
            # 多季分享里裸集数归不到 season 就算孤儿,不再默认 1
            if has_multi_folder_season:
                orphans.append(v)
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
            skipped=[v.name for v in orphans],
        )

    added: list[str] = []
    skipped: list[str] = [v.name for v in orphans]
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
        existing_names = {f.name for f in existing_files}

        for ss, ep, v in parsed:
            if ss != s:
                continue
            ep_list = ep if isinstance(ep, list) else [ep]
            try:
                ep_ints = [int(e) for e in ep_list]
            except (TypeError, ValueError):
                skipped.append(v.name)
                continue

            if any(e in existing_eps for e in ep_ints):
                skipped.append(v.name)
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
            existing_names.add(new_name)

            # 每集 NFO + thumb
            if cfg.policy.write_metadata and tmdb is not None and tmdb_id is not None:
                try:
                    _write_episode_metadata(
                        qc, tmdb, tmdb_id, s, ep_ints,
                        season_fid, new_name, existing_names,
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
    )
