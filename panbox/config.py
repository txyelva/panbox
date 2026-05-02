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
    library_variety: str = ""


@dataclass
class AliConfig:
    refresh_token: str = ""
    staging_movies: str = ""
    staging_tv: str = ""
    library_movies: str = ""
    library_tv: str = ""
    library_variety: str = ""


@dataclass
class Drive115Config:
    cookie: str = ""
    staging_movies: str = ""
    staging_tv: str = ""
    library_movies: str = ""
    library_tv: str = ""
    library_variety: str = ""


@dataclass
class BaiduConfig:
    cookie: str = ""
    staging_movies: str = ""
    staging_tv: str = ""
    library_movies: str = ""
    library_tv: str = ""
    library_variety: str = ""


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
        cfg = cls(
            tmdb=TMDBConfig(**raw["tmdb"]),
            quark=QuarkConfig(**quark_raw),
            ali=AliConfig(**ali_raw),
            drive115=Drive115Config(**d115_raw),
            baidu=BaiduConfig(**baidu_raw),
            policy=PolicyConfig(**(raw.get("policy") or {})),
        )
        cfg._validate()
        return cfg

    def _validate(self) -> None:
        """校验已配置凭据的云盘路径字段不能为空。"""
        _PATH_FIELDS = ("staging_movies", "staging_tv", "library_movies", "library_tv")
        checks = [
            ("quark",   self.quark,   self.quark.cookie),
            ("ali",     self.ali,     self.ali.refresh_token),
            ("115",     self.drive115, self.drive115.cookie),
            ("baidu",   self.baidu,   self.baidu.cookie),
        ]
        errors = []
        for name, cloud_cfg, credential in checks:
            if not credential:
                continue  # 未启用该云盘,跳过
            for field in _PATH_FIELDS:
                val = getattr(cloud_cfg, field, None)
                if not val or not isinstance(val, str):
                    errors.append(f"clouds.{name}.{field} 未设置(凭据已填但路径为空)")
        if errors:
            raise ValueError("配置校验失败:\n  " + "\n  ".join(errors))
