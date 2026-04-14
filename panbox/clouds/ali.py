"""阿里云盘(alipan / aliyundrive)适配器。

认证走 refresh_token:
  POST https://auth.aliyundrive.com/v2/account/token
    {grant_type: refresh_token, refresh_token: ...}
  → access_token / user_id / default_drive_id

所有自家盘接口都带 drive_id,根目录 file_id 为 "root"。
我们对外把 "0" 映射成 "root",让 pipeline 跨云盘一致。
"""
from __future__ import annotations

import re
import time
from typing import Any, Iterable, Optional

import requests

from .base import Cloud, CloudError, RemoteFile

AUTH = "https://auth.aliyundrive.com"
API = "https://api.aliyundrive.com"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def parse_share_url(url: str) -> tuple[str, Optional[str]]:
    """返回 (share_id, passcode)。密码可能在 ?pwd= 里。"""
    m = re.search(r"(?:alipan|aliyundrive)\.com/s/([A-Za-z0-9_-]+)", url)
    if not m:
        raise CloudError(f"不是有效的阿里云盘分享链接: {url}")
    share_id = m.group(1)
    pw: Optional[str] = None
    m2 = re.search(r"[?&]pwd=([A-Za-z0-9]+)", url)
    if m2:
        pw = m2.group(1)
    return share_id, pw


class AliClient(Cloud):
    name = "ali"

    def __init__(self, refresh_token: str, request_interval: float = 0.25):
        if not refresh_token:
            raise CloudError("阿里云盘 refresh_token 为空")
        self._refresh_token = refresh_token
        self._access_token: str = ""
        self._expires_at: float = 0
        self.drive_id: str = ""
        self.user_id: str = ""
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": UA,
            "Referer": "https://www.alipan.com/",
            "Origin": "https://www.alipan.com",
            "Content-Type": "application/json;charset=UTF-8",
            "Accept": "application/json, text/plain, */*",
        })
        self.interval = request_interval
        self._path_cache: dict[str, str] = {"/": "root", "": "root"}
        self._refresh_access_token()

    # -------------------- 认证 --------------------
    def _refresh_access_token(self) -> None:
        r = requests.post(
            f"{AUTH}/v2/account/token",
            json={
                "refresh_token": self._refresh_token,
                "grant_type": "refresh_token",
            },
            headers={"Content-Type": "application/json;charset=UTF-8"},
            timeout=15,
        )
        if r.status_code >= 400:
            raise CloudError(f"refresh_token 失败 {r.status_code}: {r.text[:200]}")
        data = r.json()
        self._access_token = data["access_token"]
        self._refresh_token = data.get("refresh_token") or self._refresh_token
        self.drive_id = str(data.get("default_drive_id") or "")
        self.user_id = str(data.get("user_id") or "")
        # access_token 一般 2h 有效,留 5 分钟余量
        self._expires_at = time.time() + float(data.get("expires_in", 7200)) - 300
        self.session.headers["Authorization"] = f"Bearer {self._access_token}"

    def _ensure_token(self) -> None:
        if time.time() >= self._expires_at:
            self._refresh_access_token()

    # -------------------- HTTP --------------------
    def _post(
        self,
        path: str,
        body: dict,
        *,
        share_token: Optional[str] = None,
        host: str = API,
    ) -> dict:
        self._ensure_token()
        headers: dict[str, str] = {}
        if share_token:
            headers["x-share-token"] = share_token
        r = self.session.post(
            f"{host}{path}", json=body, headers=headers or None, timeout=20
        )
        time.sleep(self.interval)
        if r.status_code == 401:
            # token 刚好过期,刷新一次重试
            self._refresh_access_token()
            if share_token:
                headers["x-share-token"] = share_token
            r = self.session.post(
                f"{host}{path}", json=body, headers=headers or None, timeout=20
            )
        if r.status_code >= 400:
            raise CloudError(f"{path} HTTP {r.status_code}: {r.text[:400]}")
        if not r.content:
            return {}
        return r.json()

    # -------------------- id 映射 --------------------
    @staticmethod
    def _to_ali(pdir_fid: str) -> str:
        return "root" if pdir_fid in ("0", "", "/") else pdir_fid

    def _to_remote_file(self, x: dict, parent: str) -> RemoteFile:
        is_dir = x.get("type") == "folder"
        return RemoteFile(
            fid=x["file_id"],
            name=x.get("name", ""),
            is_dir=is_dir,
            size=int(x.get("size") or 0),
            parent_fid=parent,
            fid_token=None,  # 阿里不需要,batch 调用带 share_id 即可
        )

    # -------------------- 分享 --------------------
    def get_stoken(self, pwd_id: str, passcode: str = "") -> str:
        data = self._post(
            "/v2/share_link/get_share_token",
            {"share_id": pwd_id, "share_pwd": passcode or ""},
        )
        stoken = data.get("share_token")
        if not stoken:
            raise CloudError(f"get_share_token 失败: {data}")
        return stoken

    def list_share(
        self, pwd_id: str, stoken: str, pdir_fid: str = "0"
    ) -> list[RemoteFile]:
        parent = self._to_ali(pdir_fid)
        out: list[RemoteFile] = []
        marker = ""
        while True:
            body: dict[str, Any] = {
                "share_id": pwd_id,
                "parent_file_id": parent,
                "limit": 100,
                "order_by": "name",
                "order_direction": "DESC",
            }
            if marker:
                body["marker"] = marker
            data = self._post("/adrive/v3/file/list", body, share_token=stoken)
            for item in data.get("items", []) or []:
                out.append(self._to_remote_file(item, parent))
            marker = data.get("next_marker") or ""
            if not marker:
                break
        return out

    def list_share_recursive(
        self, pwd_id: str, stoken: str, pdir_fid: str = "0"
    ) -> list[RemoteFile]:
        out: list[RemoteFile] = []
        stack = [pdir_fid]
        while stack:
            cur = stack.pop()
            items = self.list_share(pwd_id, stoken, cur)
            for f in items:
                out.append(f)
                if f.is_dir:
                    stack.append(f.fid)
        return out

    def save_share(
        self,
        pwd_id: str,
        stoken: str,
        fid_list: list[str],
        fid_token_list: list[str],
        to_pdir_fid: str,
    ) -> list[str]:
        """批量 copy 分享文件到自家盘。fid_token_list 在阿里云盘未使用。"""
        if not fid_list:
            return []
        to_parent = self._to_ali(to_pdir_fid)
        requests_body: list[dict] = []
        for i, fid in enumerate(fid_list):
            requests_body.append({
                "body": {
                    "file_id": fid,
                    "share_id": pwd_id,
                    "auto_rename": True,
                    "to_parent_file_id": to_parent,
                    "to_drive_id": self.drive_id,
                },
                "headers": {"Content-Type": "application/json"},
                "id": str(i),
                "method": "POST",
                "url": "/file/copy",
            })
        data = self._post(
            "/adrive/v2/batch",
            {"requests": requests_body, "resource": "file"},
            share_token=stoken,
        )
        new_fids: list[str] = []
        for resp in data.get("responses") or []:
            status = resp.get("status", 0)
            if 200 <= status < 300:
                b = resp.get("body") or {}
                fid = b.get("file_id") or b.get("domain_id")
                if fid:
                    new_fids.append(fid)
            else:
                # 单个失败不中断,但记录
                pass
        if not new_fids:
            raise CloudError(f"save_share 全部失败: {data}")
        return new_fids

    # -------------------- 自家文件 --------------------
    def list_dir(self, pdir_fid: str) -> list[RemoteFile]:
        parent = self._to_ali(pdir_fid)
        out: list[RemoteFile] = []
        marker = ""
        while True:
            body: dict[str, Any] = {
                "drive_id": self.drive_id,
                "parent_file_id": parent,
                "limit": 100,
                "order_by": "updated_at",
                "order_direction": "DESC",
                "fields": "*",
            }
            if marker:
                body["marker"] = marker
            data = self._post("/adrive/v3/file/list", body)
            for item in data.get("items", []) or []:
                out.append(self._to_remote_file(item, parent))
            marker = data.get("next_marker") or ""
            if not marker:
                break
        return out

    def list_dir_recursive(self, pdir_fid: str) -> list[RemoteFile]:
        out: list[RemoteFile] = []
        stack = [pdir_fid]
        while stack:
            cur = stack.pop()
            for f in self.list_dir(cur):
                out.append(f)
                if f.is_dir:
                    stack.append(f.fid)
        return out

    def mkdir(self, parent_fid: str, name: str) -> str:
        parent = self._to_ali(parent_fid)
        data = self._post(
            "/adrive/v2/file/createWithFolders",
            {
                "drive_id": self.drive_id,
                "parent_file_id": parent,
                "name": name,
                "type": "folder",
                "check_name_mode": "refuse",
            },
        )
        fid = data.get("file_id")
        if not fid:
            raise CloudError(f"mkdir 失败: {data}")
        return fid

    def mkdir_p(self, path: str) -> str:
        path = path.strip()
        if not path or path == "/":
            return "root"
        if path in self._path_cache:
            return self._path_cache[path]
        segs = [s for s in path.split("/") if s]
        cur_fid = "root"
        cur_path = ""
        for seg in segs:
            cur_path = f"{cur_path}/{seg}"
            if cur_path in self._path_cache:
                cur_fid = self._path_cache[cur_path]
                continue
            children = self.list_dir(cur_fid)
            hit = next((c for c in children if c.is_dir and c.name == seg), None)
            if hit is None:
                cur_fid = self.mkdir(cur_fid, seg)
            else:
                cur_fid = hit.fid
            self._path_cache[cur_path] = cur_fid
        return cur_fid

    def resolve_path(self, path: str) -> Optional[str]:
        path = path.strip()
        if not path or path == "/":
            return "root"
        if path in self._path_cache:
            return self._path_cache[path]
        segs = [s for s in path.split("/") if s]
        cur_fid = "root"
        cur_path = ""
        for seg in segs:
            cur_path = f"{cur_path}/{seg}"
            if cur_path in self._path_cache:
                cur_fid = self._path_cache[cur_path]
                continue
            children = self.list_dir(cur_fid)
            hit = next((c for c in children if c.is_dir and c.name == seg), None)
            if hit is None:
                return None
            cur_fid = hit.fid
            self._path_cache[cur_path] = cur_fid
        return cur_fid

    def rename(self, fid: str, new_name: str) -> None:
        self._post(
            "/v3/file/update",
            {
                "drive_id": self.drive_id,
                "file_id": fid,
                "name": new_name,
                "check_name_mode": "refuse",
            },
        )

    def move(self, fids: Iterable[str], to_pdir_fid: str) -> None:
        fids = list(fids)
        if not fids:
            return
        to_parent = self._to_ali(to_pdir_fid)
        requests_body: list[dict] = []
        for i, fid in enumerate(fids):
            requests_body.append({
                "body": {
                    "drive_id": self.drive_id,
                    "file_id": fid,
                    "to_drive_id": self.drive_id,
                    "to_parent_file_id": to_parent,
                },
                "headers": {"Content-Type": "application/json"},
                "id": str(i),
                "method": "POST",
                "url": "/file/move",
            })
        self._post(
            "/adrive/v2/batch",
            {"requests": requests_body, "resource": "file"},
        )

    def delete(self, fids: Iterable[str]) -> None:
        fids = list(fids)
        if not fids:
            return
        requests_body: list[dict] = []
        for i, fid in enumerate(fids):
            requests_body.append({
                "body": {
                    "drive_id": self.drive_id,
                    "file_id": fid,
                },
                "headers": {"Content-Type": "application/json"},
                "id": str(i),
                "method": "POST",
                "url": "/recyclebin/trash",
            })
        self._post(
            "/adrive/v2/batch",
            {"requests": requests_body, "resource": "file"},
        )

    # -------------------- 小文件上传 --------------------
    def upload_bytes(
        self,
        parent_fid: str,
        name: str,
        data: bytes,
        mime: str = "application/octet-stream",
    ) -> Optional[str]:
        """create → PUT upload_url → complete。适合 NFO / 海报(单 part)。"""
        parent = self._to_ali(parent_fid)
        size = len(data)
        pre = self._post(
            "/adrive/v2/file/createWithFolders",
            {
                "drive_id": self.drive_id,
                "parent_file_id": parent,
                "name": name,
                "type": "file",
                "check_name_mode": "auto_rename",
                "size": size,
                "part_info_list": [{"part_number": 1}],
            },
        )
        file_id = pre.get("file_id")
        upload_id = pre.get("upload_id")
        parts = pre.get("part_info_list") or []
        if not (file_id and upload_id and parts):
            raise CloudError(f"阿里 createWithFolders 返回异常: {pre}")
        if pre.get("rapid_upload"):
            return file_id
        upload_url = parts[0].get("upload_url")
        if not upload_url:
            raise CloudError(f"阿里 createWithFolders 没返回 upload_url: {pre}")

        put = requests.put(
            upload_url,
            data=data,
            headers={"Content-Type": ""},
            timeout=60,
        )
        if put.status_code >= 400:
            raise CloudError(
                f"阿里 OSS PUT 失败 {put.status_code}: {put.text[:300]}"
            )

        self._post(
            "/v2/file/complete",
            {
                "drive_id": self.drive_id,
                "file_id": file_id,
                "upload_id": upload_id,
            },
        )
        return file_id
