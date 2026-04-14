from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional, Protocol, runtime_checkable

VIDEO_EXTS = {
    "mkv", "mp4", "ts", "m2ts", "avi", "mov", "wmv", "flv",
    "rmvb", "rm", "iso", "webm", "m4v", "mpg", "mpeg",
}


@dataclass
class RemoteFile:
    fid: str
    name: str
    is_dir: bool
    size: int = 0
    parent_fid: Optional[str] = None
    fid_token: Optional[str] = None      # 分享列表里才有

    @property
    def ext(self) -> str:
        if "." not in self.name:
            return ""
        return self.name.rsplit(".", 1)[-1].lower()

    @property
    def is_video(self) -> bool:
        return (not self.is_dir) and self.ext in VIDEO_EXTS


@runtime_checkable
class Cloud(Protocol):
    """所有云盘适配器需要实现的接口。pipeline 只依赖这里的方法。

    术语对齐:
    - pwd_id / stoken 在不同网盘叫法不同(夸克 pwd_id+stoken,阿里 share_id+share_token)。
      统一用 pwd_id 指"分享 ID",stoken 指"分享凭据"。
    - fid 是文件/目录的云端 ID(阿里的 file_id、夸克的 fid)。
    - root fid 约定用 "0" 表示(ali 内部用 "root",由适配器自己映射)。
    """

    name: str  # "quark" | "ali" | ...

    # -------- 分享 --------
    def get_stoken(self, pwd_id: str, passcode: str = "") -> str: ...

    def list_share(
        self, pwd_id: str, stoken: str, pdir_fid: str = "0"
    ) -> list[RemoteFile]: ...

    def list_share_recursive(
        self, pwd_id: str, stoken: str, pdir_fid: str = "0"
    ) -> list[RemoteFile]: ...

    def save_share(
        self,
        pwd_id: str,
        stoken: str,
        fid_list: list[str],
        fid_token_list: list[str],
        to_pdir_fid: str,
    ) -> list[str]: ...

    # -------- 自家盘 --------
    def list_dir(self, pdir_fid: str) -> list[RemoteFile]: ...

    def list_dir_recursive(self, pdir_fid: str) -> list[RemoteFile]: ...

    def mkdir(self, parent_fid: str, name: str) -> str: ...

    def mkdir_p(self, path: str) -> str: ...

    def resolve_path(self, path: str) -> Optional[str]: ...

    def rename(self, fid: str, new_name: str) -> None: ...

    def move(self, fids: Iterable[str], to_pdir_fid: str) -> None: ...

    def delete(self, fids: Iterable[str]) -> None: ...

    # -------- 小文件上传(NFO / 海报 / 缩略图) --------
    def upload_bytes(
        self,
        parent_fid: str,
        name: str,
        data: bytes,
        mime: str = "application/octet-stream",
    ) -> Optional[str]: ...


class CloudError(RuntimeError):
    pass
