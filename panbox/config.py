from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_CONFIG_PATH = Path(
    os.environ.get(
        "PANBOX_CONFIG",
        str(Path.home() / ".config" / "panbox" / "config.yaml"),
    )
)


@dataclass
class TMDBConfig:
    api_key: str
    language: str = "zh-CN"


@dataclass
class QuarkConfig:
    cookie: str = ""
    staging_movies: str = ""
    staging_tv: str = ""
    library_movies: str = ""
    library_tv: str = ""


@dataclass
class AliConfig:
    refresh_token: str = ""
    staging_movies: str = ""
    staging_tv: str = ""
    library_movies: str = ""
    library_tv: str = ""


@dataclass
class Drive115Config:
    cookie: str = ""
    staging_movies: str = ""
    staging_tv: str = ""
    library_movies: str = ""
    library_tv: str = ""


@dataclass
class BaiduConfig:
    cookie: str = ""
    staging_movies: str = ""
    staging_tv: str = ""
    library_movies: str = ""
    library_tv: str = ""


@dataclass
class PolicyConfig:
    on_movie_exists: str = "skip"
    on_tv_incomplete: str = "diff_only"
    rejected_dir_movies: str = ""
    rejected_dir_tv: str = ""
    ask_when_ambiguous: bool = True
    write_metadata: bool = True


@dataclass
class Config:
    tmdb: TMDBConfig
    quark: QuarkConfig = field(default_factory=QuarkConfig)
    ali: AliConfig = field(default_factory=AliConfig)
    drive115: Drive115Config = field(default_factory=Drive115Config)
    baidu: BaiduConfig = field(default_factory=BaiduConfig)
    policy: PolicyConfig = field(default_factory=PolicyConfig)

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        p = Path(path or DEFAULT_CONFIG_PATH)
        if not p.exists():
            raise FileNotFoundError(
                f"配置文件不存在: {p}\n运行 `panbox config init` 生成模板"
            )
        raw = yaml.safe_load(p.read_text()) or {}
        if "tmdb" not in raw:
            raise ValueError("配置缺少 tmdb 段")
        clouds = raw.get("clouds") or {}
        quark_raw = clouds.get("quark") or {}
        ali_raw = clouds.get("ali") or {}
        d115_raw = clouds.get("115") or clouds.get(115) or {}
        baidu_raw = clouds.get("baidu") or {}
        return cls(
            tmdb=TMDBConfig(**raw["tmdb"]),
            quark=QuarkConfig(**quark_raw),
            ali=AliConfig(**ali_raw),
            drive115=Drive115Config(**d115_raw),
            baidu=BaiduConfig(**baidu_raw),
            policy=PolicyConfig(**(raw.get("policy") or {})),
        )
