from __future__ import annotations

import json
import sys
from dataclasses import asdict as _asdict

import click
from rich.console import Console
from rich.table import Table

from .clouds import parse_share_url as cloud_parse_share_url
from .config import Config, DEFAULT_CONFIG_PATH
from .matcher import Guess, normalize_query
from .pipeline import ingest as _ingest
from .scraper.tmdb import TMDB

console = Console()

EXAMPLE_CONFIG = """\
tmdb:
  api_key: ""
  language: zh-CN

clouds:
  quark:
    cookie: ""
    # 电视剧与电影的待刮削目录、库目录分别设置
    staging_movies: /影视剧/待刮削/待刮削电影
    staging_tv:     /影视剧/待刮削/待刮削电视剧
    library_movies: /影视剧/Movies
    library_tv:     /影视剧/TV
  ali:
    refresh_token: ""
    staging_movies: /待刮削/电影
    staging_tv:     /待刮削/剧集
    library_movies: /影视剧/Movies
    library_tv:     /影视剧/TV
  115:
    cookie: ""
    staging_movies: /待刮削/电影
    staging_tv:     /待刮削/剧集
    library_movies: /影视剧/Movies
    library_tv:     /影视剧/TV
  baidu:
    cookie: ""
    staging_movies: /待刮削/电影
    staging_tv:     /待刮削/剧集
    library_movies: /影视剧/Movies
    library_tv:     /影视剧/TV

policy:
  on_movie_exists: skip          # skip | replace
  on_tv_incomplete: diff_only    # diff_only | all | ask
  rejected_dir_movies: /待刮削/_rejected/movies
  rejected_dir_tv:     /待刮削/_rejected/tv
  ask_when_ambiguous: true
  write_metadata: true
"""


@click.group()
@click.version_option()
def main() -> None:
    """panbox — 网盘分享 → 刮削 → 影视库入库"""


@main.command()
@click.option("--name", help="剧名或电影名(可含季度提示)")
@click.option("--file", "filename", help="文件名,用 guessit 解析后再查 TMDB")
@click.option("--type", "media_type", type=click.Choice(["movie", "tv"]))
@click.option("--json", "as_json", is_flag=True, help="输出 JSON(给 agent 用)")
def identify(
    name: str | None,
    filename: str | None,
    media_type: str | None,
    as_json: bool,
) -> None:
    """用 guessit + TMDB 识别剧/电影(不做转存)"""
    if not name and not filename:
        raise click.UsageError("至少提供 --name 或 --file")

    cfg = Config.load()
    tmdb = TMDB(cfg.tmdb.api_key, cfg.tmdb.language)

    guess: Guess | None = None
    season_hint: int | None = None
    if filename:
        guess = Guess.from_text(filename)
        query = guess.title or filename
        year = guess.year
        mt = media_type or guess.media_type
        season_hint = guess.season
    else:
        query, season_hint = normalize_query(name or "")
        year = None
        mt = media_type
        if season_hint and not mt:
            mt = "tv"

    try:
        results = tmdb.search(query, year=year, media_type=mt)[:5]
    except Exception as e:
        if as_json:
            click.echo(json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False))
        else:
            console.print(f"[red]TMDB 请求失败:[/red] {e}")
        sys.exit(1)

    payload = {
        "status": "ok",
        "query": query,
        "season": season_hint,
        "guess": asdict(guess) if guess else None,
        "candidates": [
            {
                "tmdb_id": r.id,
                "type": r.media_type,
                "title": r.title,
                "original_title": r.original_title,
                "year": r.year,
                "popularity": round(r.popularity, 1),
                "overview": r.overview[:100],
            }
            for r in results
        ],
    }

    if as_json:
        click.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    if guess:
        console.print(f"[dim]guessit:[/dim] {guess.raw}")
    console.print(
        f"[bold]Query:[/bold] {query}  [dim]year={year} type={mt or 'auto'}[/dim]"
    )

    if not results:
        console.print("[red]无候选结果[/red]")
        sys.exit(2)

    table = Table(show_header=True, header_style="bold")
    table.add_column("#")
    table.add_column("TMDB")
    table.add_column("类型")
    table.add_column("标题")
    table.add_column("年份")
    table.add_column("热度")
    for i, r in enumerate(results, 1):
        table.add_row(
            str(i),
            str(r.id),
            r.media_type,
            r.title,
            r.year or "-",
            f"{r.popularity:.1f}",
        )
    console.print(table)


@main.command()
@click.argument("url")
@click.option("--hint", help="剧名或电影名提示(如 '凡人修仙传 第二季')")
@click.option("--type", "media_type", type=click.Choice(["movie", "tv"]))
@click.option("--passcode", help="分享提取码")
@click.option("--yes", "auto_yes", is_flag=True, help="自动选择热度最高的 TMDB 结果")
@click.option("--dry-run", is_flag=True, help="只识别+输出目标路径,不转存")
@click.option("--json", "as_json", is_flag=True)
def ingest(
    url: str,
    hint: str | None,
    media_type: str | None,
    passcode: str | None,
    auto_yes: bool,
    dry_run: bool,
    as_json: bool,
) -> None:
    """把网盘分享链接转存、识别、落库(夸克 / 阿里云盘自动识别)"""
    cfg = Config.load()
    try:
        cloud_name, _, _ = cloud_parse_share_url(url)
    except Exception as e:
        raise click.UsageError(str(e))
    if cloud_name == "quark" and not cfg.quark.cookie:
        raise click.UsageError("夸克 cookie 未设置,编辑 " + str(DEFAULT_CONFIG_PATH))
    if cloud_name == "ali" and not cfg.ali.refresh_token:
        raise click.UsageError("阿里云盘 refresh_token 未设置,编辑 " + str(DEFAULT_CONFIG_PATH))
    if cloud_name == "115" and not cfg.drive115.cookie:
        raise click.UsageError("115 cookie 未设置,编辑 " + str(DEFAULT_CONFIG_PATH))
    if cloud_name == "baidu" and not cfg.baidu.cookie:
        raise click.UsageError("百度网盘 cookie 未设置,编辑 " + str(DEFAULT_CONFIG_PATH))
    try:
        result = _ingest(
            cfg,
            url,
            hint=hint,
            media_type=media_type,
            passcode=passcode,
            auto_yes=auto_yes,
            dry_run=dry_run,
        )
    except Exception as e:
        if as_json:
            click.echo(json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False))
        else:
            console.print(f"[red]失败:[/red] {e}")
        sys.exit(1)

    payload = _asdict(result)
    if as_json:
        click.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    color = {
        "ok": "green",
        "need_confirm": "yellow",
        "skipped": "yellow",
        "error": "red",
    }.get(result.status, "white")
    console.print(f"[{color}]{result.status}[/{color}] "
                  f"{result.type or ''} {result.title or ''} "
                  f"({result.year or '-'}) tmdb={result.tmdb_id or '-'}")
    if result.path:
        console.print(f"目标: {result.path}")
    if result.added:
        console.print(f"[green]入库 {len(result.added)}[/green]: " + ", ".join(result.added))
    if result.skipped:
        console.print(f"[yellow]跳过 {len(result.skipped)}[/yellow]: " + ", ".join(result.skipped))
    if result.candidates:
        console.print("[yellow]需要确认,候选:[/yellow]")
        for i, c in enumerate(result.candidates, 1):
            console.print(f"  {i}. {c['title']} ({c.get('year','-')}) [{c['type']}] "
                          f"tmdb={c['tmdb_id']} 热度={c['popularity']}")
        console.print("  → 重跑时加 --yes 选第一个,或 --hint 缩小范围")
    if result.message:
        console.print(f"[dim]{result.message}[/dim]")


@main.command()
def doctor() -> None:
    """检查环境:配置、TMDB 连通性、目录设置"""
    try:
        cfg = Config.load()
    except Exception as e:
        console.print(f"[red]✗[/red] {e}")
        sys.exit(1)
    console.print(f"[green]✓[/green] 配置已加载: {DEFAULT_CONFIG_PATH}")

    if not cfg.tmdb.api_key:
        console.print("[red]✗[/red] TMDB api_key 为空")
        sys.exit(1)
    tmdb = TMDB(cfg.tmdb.api_key, cfg.tmdb.language)
    if tmdb.ping():
        console.print("[green]✓[/green] TMDB API 连通")
    else:
        console.print("[red]✗[/red] TMDB API 不通(检查 key 或网络)")
        sys.exit(1)

    def _print_cloud(label: str, cloud_cfg, cred_label: str, cred_val: str) -> None:
        console.print(f"\n[bold]{label}[/bold]")
        mark = "[green]✓[/green]" if cred_val else "[yellow]![/yellow]"
        console.print(f"  {mark} {cred_label}: {'已设置' if cred_val else '(未设置)'}")
        for name, val in [
            ("staging_movies", cloud_cfg.staging_movies),
            ("staging_tv    ", cloud_cfg.staging_tv),
            ("library_movies", cloud_cfg.library_movies),
            ("library_tv    ", cloud_cfg.library_tv),
        ]:
            mark = "[green]✓[/green]" if val else "[yellow]![/yellow]"
            console.print(f"  {mark} {name}: {val or '(未设置)'}")

    _print_cloud("夸克", cfg.quark, "cookie", cfg.quark.cookie)
    _print_cloud("阿里云盘", cfg.ali, "refresh_token", cfg.ali.refresh_token)
    _print_cloud("115", cfg.drive115, "cookie", cfg.drive115.cookie)
    _print_cloud("百度网盘", cfg.baidu, "cookie", cfg.baidu.cookie)


@main.group()
def config() -> None:
    """配置管理"""


@config.command("init")
@click.option("--force", is_flag=True, help="覆盖已有配置")
@click.option("--tmdb-key", help="直接写入 TMDB API key")
def config_init(force: bool, tmdb_key: str | None) -> None:
    """生成示例配置到 ~/.config/panbox/config.yaml"""
    path = DEFAULT_CONFIG_PATH
    if path.exists() and not force:
        console.print(f"[yellow]已存在:[/yellow] {path} (用 --force 覆盖)")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    content = EXAMPLE_CONFIG
    if tmdb_key:
        content = content.replace('api_key: ""', f'api_key: "{tmdb_key}"')
    path.write_text(content)
    console.print(f"[green]✓[/green] 已写入 {path}")


@config.command("path")
def config_path() -> None:
    """打印配置文件路径"""
    click.echo(str(DEFAULT_CONFIG_PATH))


if __name__ == "__main__":
    main()
