"""云盘适配器工厂。

根据 URL 前缀选择实现,不要让 pipeline 硬绑某个具体客户端。
新加网盘只需:
  1. 在 clouds/ 下新建一个模块,实现 Cloud Protocol
  2. 在 REGISTRY 里登记 (host 正则, factory)
"""
from __future__ import annotations

import re
from typing import Callable, Optional

from ..config import Config
from .base import Cloud, CloudError, RemoteFile, VIDEO_EXTS

__all__ = [
    "Cloud", "CloudError", "RemoteFile", "VIDEO_EXTS",
    "from_url", "by_name", "parse_share_url",
]


# (正则, 懒加载工厂),用闭包避免未用到的云盘连模块都要导入
REGISTRY: list[tuple[re.Pattern[str], Callable[[Config], Cloud], str]] = []


def _register_quark() -> None:
    def factory(cfg: Config) -> Cloud:
        from .quark import QuarkClient
        return QuarkClient(cfg.quark.cookie)
    REGISTRY.append((re.compile(r"pan\.quark\.cn/s/"), factory, "quark"))


def _register_ali() -> None:
    def factory(cfg: Config) -> Cloud:
        from .ali import AliClient
        return AliClient(cfg.ali.refresh_token)
    REGISTRY.append(
        (re.compile(r"(alipan\.com|aliyundrive\.com)/s/"), factory, "ali"),
    )


def _register_115() -> None:
    def factory(cfg: Config) -> Cloud:
        from .drive115 import Drive115Client
        return Drive115Client(cfg.drive115.cookie)
    REGISTRY.append(
        (re.compile(r"115(?:cdn)?\.com/s/"), factory, "115"),
    )


def _register_baidu() -> None:
    def factory(cfg: Config) -> Cloud:
        from .baidu import BaiduClient
        return BaiduClient(cfg.baidu.cookie)
    REGISTRY.append(
        (re.compile(r"pan\.baidu\.com/s/"), factory, "baidu"),
    )


_register_quark()
_register_ali()
_register_115()
_register_baidu()


def from_url(url: str, cfg: Config) -> tuple[Cloud, str]:
    """根据分享链接选一个 Cloud 实现,返回 (client, cloud_name)。"""
    for pat, factory, name in REGISTRY:
        if pat.search(url):
            return factory(cfg), name
    raise CloudError(f"不支持的分享链接: {url}")


def by_name(name: str, cfg: Config) -> Cloud:
    """按名字拿 Cloud(给 CLI doctor 等用)。"""
    for _, factory, n in REGISTRY:
        if n == name:
            return factory(cfg)
    raise CloudError(f"未知云盘: {name}")


def parse_share_url(url: str) -> tuple[str, str, Optional[str]]:
    """统一的分享链接解析。返回 (cloud_name, pwd_id, passcode)。"""
    if re.search(r"pan\.quark\.cn/s/", url):
        from .quark import parse_share_url as p
        pwd_id, pw = p(url)
        return "quark", pwd_id, pw
    if re.search(r"(alipan\.com|aliyundrive\.com)/s/", url):
        from .ali import parse_share_url as p
        pwd_id, pw = p(url)
        return "ali", pwd_id, pw
    if re.search(r"115(?:cdn)?\.com/s/", url):
        from .drive115 import parse_share_url as p
        pwd_id, pw = p(url)
        return "115", pwd_id, pw
    if re.search(r"pan\.baidu\.com/s/", url):
        from .baidu import parse_share_url as p
        pwd_id, pw = p(url)
        return "baidu", pwd_id, pw
    raise CloudError(f"不支持的分享链接: {url}")
