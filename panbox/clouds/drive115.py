"""115 网盘适配器。

认证走 cookie(网页登录后 F12 拿 UID/CID/SEID):
  Cookie: UID=xxx; CID=xxx; SEID=xxx

分享链接格式:
  https://115.com/s/SHARE_CODE?pwd=PASSCODE
  https://115.com/s/SHARE_CODE#PASSCODE  (旧格式)

stoken 约定: "{share_code}:{receive_code}" — 把两字段打包,方便 Protocol 复用。

上传走 OSS 三段式(预检 → PUT → 完成),适合 NFO/poster 小文件。
"""
from __future__ import annotations

import hashlib
import re
import time
from typing import Any, Iterable, Optional

import requests

from .base import Cloud, CloudError, RemoteFile

API = "https://webapi.115.com"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def parse_share_url(url: str) -> tuple[str, Optional[str]]:
    """返回 (share_code, passcode)。支持 115.com 和 115cdn.com。"""
    m = re.search(r"115(?:cdn)?\.com/s/([A-Za-z0-9_-]+)", url)
    if not m:
        raise CloudError(f"不是有效的 115 分享链接: {url}")
    share_code = m.group(1)
    # ?pwd=XXXX 或 ?password=XXXX 或 #XXXX(旧格式)
    pw: Optional[str] = None
    m2 = re.search(r"[?&](?:pwd|password)=([A-Za-z0-9]+)", url)
    if m2:
        pw = m2.group(1)
    else:
        m3 = re.search(r"#([A-Za-z0-9]{4,})$", url)
        if m3:
            pw = m3.group(1)
    return share_code, pw


class Drive115Client(Cloud):
    name = "115"

    def __init__(self, cookie: str, request_interval: float = 0.3):
        if not cookie:
            raise CloudError("115 cookie 为空")
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": UA,
            "Referer": "https://115.com/",
            "Origin": "https://115.com",
            "Accept": "application/json, text/plain, */*",
            "Cookie": cookie,
        })
        self.interval = request_interval
        self._path_cache: dict[str, str] = {"": "0", "/": "0"}

    # -------------------- HTTP --------------------

    def _get(self, path: str, params: dict | None = None) -> dict:
        r = self.session.get(f"{API}{path}", params=params, timeout=20)
        time.sleep(self.interval)
        if r.status_code >= 400:
            raise CloudError(f"GET {path} HTTP {r.status_code}: {r.text[:400]}")
        data = r.json()
        if not data.get("state", True):
            raise CloudError(f"GET {path} 返回 state=false: {data}")
        return data

    def _post(self, path: str, body: dict, ignore_errno: tuple[int, ...] = ()) -> dict:
        r = self.session.post(f"{API}{path}", data=body, timeout=20)
        time.sleep(self.interval)
        if r.status_code >= 400:
            raise CloudError(f"POST {path} HTTP {r.status_code}: {r.text[:400]}")
        data = r.json()
        if not data.get("state", True):
            if data.get("errno") in ignore_errno:
                return data
            raise CloudError(f"POST {path} 返回 state=false: {data}")
        return data

    # -------------------- stoken 打包 --------------------

    @staticmethod
    def _pack_stoken(share_code: str, receive_code: str) -> str:
        return f"{share_code}:{receive_code}"

    @staticmethod
    def _unpack_stoken(stoken: str) -> tuple[str, str]:
        if ":" not in stoken:
            return stoken, ""
        share_code, receive_code = stoken.split(":", 1)
        return share_code, receive_code

    # -------------------- 分享 --------------------

    def get_stoken(self, pwd_id: str, passcode: str = "") -> str:
        """115 不需要额外换 token,把 share_code+receive_code 打包返回。"""
        return self._pack_stoken(pwd_id, passcode)

    def list_share(
        self, pwd_id: str, stoken: str, pdir_fid: str = "0"
    ) -> list[RemoteFile]:
        share_code, receive_code = self._unpack_stoken(stoken)
        out: list[RemoteFile] = []
        offset = 0
        limit = 115
        while True:
            params: dict[str, Any] = {
                "share_code": share_code,
                "receive_code": receive_code,
                "cid": pdir_fid,
                "limit": limit,
                "offset": offset,
                "show_dir": 1,
            }
            data = self._get("/share/snap", params)
            items = (data.get("data") or {}).get("list") or []
            for item in items:
                out.append(self._to_remote_file_share(item))
            total = (data.get("data") or {}).get("count") or 0
            offset += len(items)
            if offset >= total or not items:
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
        if not fid_list:
            return []
        share_code, receive_code = self._unpack_stoken(stoken)
        body: dict[str, Any] = {
            "share_code": share_code,
            "receive_code": receive_code,
            "file_id": ",".join(fid_list),
            "cid": to_pdir_fid,
            "is_sure": 1,
        }
        # errno=4200045: "文件已接收,无需重复接收" — 视为成功,staging 里已有
        self._post("/share/receive", body, ignore_errno=(4200045,))
        # 115 不返回新 fid 列表;调用方用快照差扫 staging 获取新文件
        return []

    # -------------------- 自家文件 --------------------

    def list_dir(self, pdir_fid: str) -> list[RemoteFile]:
        out: list[RemoteFile] = []
        offset = 0
        limit = 115
        while True:
            params: dict[str, Any] = {
                "cid": pdir_fid,
                "show_dir": 1,
                "limit": limit,
                "offset": offset,
                "o": "user_ptime",
                "asc": 0,
            }
            data = self._get("/files", params)
            items = data.get("data") or []
            for item in items:
                out.append(self._to_remote_file_own(item))
            total = data.get("count") or 0
            offset += len(items)
            if offset >= total or not items:
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
        data = self._post("/files/add", {"cname": name, "pid": parent_fid})
        fid = str((data.get("file") or {}).get("file_id") or data.get("cid") or "")
        if not fid:
            raise CloudError(f"mkdir 失败,未拿到 fid: {data}")
        return fid

    def mkdir_p(self, path: str) -> str:
        path = path.strip()
        if not path or path == "/":
            return "0"
        if path in self._path_cache:
            return self._path_cache[path]
        segs = [s for s in path.split("/") if s]
        cur_fid = "0"
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
            return "0"
        if path in self._path_cache:
            return self._path_cache[path]
        segs = [s for s in path.split("/") if s]
        cur_fid = "0"
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
        self._post("/files/batch_rename", {f"files_new_name[{fid}]": new_name})

    def move(self, fids: Iterable[str], to_pdir_fid: str) -> None:
        fids_list = list(fids)
        if not fids_list:
            return
        body: dict[str, Any] = {"pid": to_pdir_fid}
        for i, fid in enumerate(fids_list):
            body[f"fid[{i}]"] = fid
        self._post("/files/move", body)

    def delete(self, fids: Iterable[str]) -> None:
        fids_list = list(fids)
        if not fids_list:
            return
        body: dict[str, Any] = {"ignore_warn": 1}
        for i, fid in enumerate(fids_list):
            body[f"fid[{i}]"] = fid
        self._post("/rb/delete", body)

    # -------------------- 上传(NFO / 海报 小文件) --------------------

    def upload_bytes(
        self,
        parent_fid: str,
        name: str,
        data: bytes,
        mime: str = "application/octet-stream",
    ) -> Optional[str]:
        """三步上传:预检(快传) → 若无法快传则走 OSS 单 part。"""
        size = len(data)
        sha1 = hashlib.sha1(data).hexdigest().upper()
        # 115 要求 pre_id = 前 128KB 的 SHA1(文件小于 128KB 时等于全文 SHA1)
        pre_id = hashlib.sha1(data[:131072]).hexdigest().upper()

        # Step 1: 预检(尝试快传 / 获取 OSS 凭据)
        init_r = self.session.post(
            "https://uplb.115.com/3.0/sampleinitupload.php",
            data={
                "userid": "",   # 服务端从 cookie 读
                "filename": name,
                "filesize": size,
                "fileid": sha1,
                "target": f"U_1_{parent_fid}",
                "sig": "",
                "t": int(time.time()),
            },
            timeout=20,
        )
        time.sleep(self.interval)
        if init_r.status_code >= 400:
            raise CloudError(f"115 upload init HTTP {init_r.status_code}: {init_r.text[:200]}")
        init_data = init_r.json()

        # status=2 → 秒传成功
        if init_data.get("status") == 2:
            return str((init_data.get("fileinfo") or {}).get("fid") or "")

        # status=1 或无 status(直接给了 OSS 凭据)→ 需要实际上传
        if init_data.get("status") not in (1, None) and "host" not in init_data:
            raise CloudError(f"115 upload init 返回异常: {init_data}")

        oss_url: str = init_data.get("host") or ""
        callback = init_data.get("callback") or {}
        osstoken = init_data.get("object") or name

        if not oss_url:
            raise CloudError(f"115 upload init 未返回 host: {init_data}")

        # Step 2: multipart/form-data 上传到 OSS
        # callback 已经是 base64 字符串,直接透传(不要再 encode)
        cb_b64 = callback if isinstance(callback, str) else ""

        fields: dict[str, Any] = {
            "key": osstoken,
            "OSSAccessKeyId": init_data.get("accessid") or "",
            "policy": init_data.get("policy") or "",
            "callback": cb_b64,
            "signature": init_data.get("signature") or "",
            "success_action_status": "200",
        }
        oss_r = self.session.post(
            oss_url,
            data={k: v for k, v in fields.items() if v},
            files={"file": (name, data, mime)},
            timeout=60,
        )
        time.sleep(self.interval)
        if oss_r.status_code >= 400:
            raise CloudError(f"115 OSS 上传失败 {oss_r.status_code}: {oss_r.text[:300]}")

        # Step 3: 上传完成通知(部分 OSS 配置走 callback 自动完成,无需额外请求)
        return None   # fid 可通过 list_dir 找到

    # -------------------- 转换 --------------------

    def _to_remote_file_share(self, x: dict) -> RemoteFile:
        # 目录:ico=="folder" 或 sha 缺失(目录没有 sha)
        is_dir = x.get("ico") == "folder" or not x.get("sha")
        # 目录用 cid 做 ID(与自家盘一致),文件用 fid
        fid = str(x["fid"]) if not is_dir else str(x.get("cid") or x.get("fid") or "")
        return RemoteFile(
            fid=fid,
            name=x.get("n") or x.get("fn") or "",
            is_dir=is_dir,
            size=int(x.get("s") or x.get("fs") or 0),
            parent_fid=str(x.get("pid") or "0"),
        )

    def _to_remote_file_own(self, x: dict) -> RemoteFile:
        # 目录条目没有 fid 字段,用 cid 做 ID;文件条目有 fid
        is_dir = "fid" not in x
        fid = str(x["fid"]) if not is_dir else str(x.get("cid") or "")
        return RemoteFile(
            fid=fid,
            name=x.get("n") or x.get("fn") or "",
            is_dir=is_dir,
            size=int(x.get("s") or x.get("fs") or 0),
            parent_fid=str(x.get("pid") or "0"),
        )
