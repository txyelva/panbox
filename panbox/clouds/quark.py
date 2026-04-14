from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from typing import Iterable, Optional

import requests

from .base import RemoteFile

API = "https://drive-pc.quark.cn/1/clouddrive"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
COMMON_PARAMS = {"pr": "ucpro", "fr": "pc"}


class QuarkError(RuntimeError):
    pass


def parse_share_url(url: str) -> tuple[str, Optional[str]]:
    """返回 (pwd_id, passcode)。passcode 可能在 ?pwd= 里。"""
    m = re.search(r"pan\.quark\.cn/s/([0-9a-zA-Z]+)", url)
    if not m:
        raise QuarkError(f"不是有效的夸克分享链接: {url}")
    pwd_id = m.group(1)
    pw = None
    m2 = re.search(r"[?&]pwd=([0-9a-zA-Z]+)", url)
    if m2:
        pw = m2.group(1)
    return pwd_id, pw


class QuarkClient:
    def __init__(self, cookie: str, request_interval: float = 0.3):
        if not cookie:
            raise QuarkError("夸克 cookie 为空")
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": UA,
            "Cookie": cookie,
            "Referer": "https://pan.quark.cn/",
            "Origin": "https://pan.quark.cn",
            "Content-Type": "application/json;charset=UTF-8",
            "Accept": "application/json, text/plain, */*",
        })
        self.interval = request_interval
        self._path_cache: dict[str, str] = {"/": "0", "": "0"}  # path -> fid

    # -------------------- HTTP --------------------
    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        p = dict(COMMON_PARAMS)
        if params:
            p.update(params)
        r = self.session.get(f"{API}{path}", params=p, timeout=20)
        time.sleep(self.interval)
        r.raise_for_status()
        data = r.json()
        if data.get("code", 0) != 0 and data.get("status", 200) >= 400:
            raise QuarkError(f"{path} failed: {data.get('message')} (code={data.get('code')})")
        return data

    def _post(self, path: str, body: dict, params: Optional[dict] = None) -> dict:
        p = dict(COMMON_PARAMS)
        if params:
            p.update(params)
        r = self.session.post(f"{API}{path}", params=p, json=body, timeout=20)
        time.sleep(self.interval)
        if r.status_code >= 400:
            raise QuarkError(
                f"{path} HTTP {r.status_code}: {r.text[:400]}"
            )
        data = r.json()
        if data.get("code", 0) != 0 and data.get("status", 200) >= 400:
            raise QuarkError(f"{path} failed: {data.get('message')} (code={data.get('code')})")
        return data

    # -------------------- 分享 --------------------
    def get_stoken(self, pwd_id: str, passcode: str = "") -> str:
        data = self._post(
            "/share/sharepage/token",
            {"pwd_id": pwd_id, "passcode": passcode or ""},
        )
        stoken = (data.get("data") or {}).get("stoken")
        if not stoken:
            raise QuarkError(f"无法获取 stoken: {data.get('message')}")
        return stoken

    def list_share(
        self,
        pwd_id: str,
        stoken: str,
        pdir_fid: str = "0",
    ) -> list[RemoteFile]:
        out: list[RemoteFile] = []
        page = 1
        while True:
            data = self._get("/share/sharepage/detail", {
                "pwd_id": pwd_id,
                "stoken": stoken,
                "pdir_fid": pdir_fid,
                "force": "0",
                "_page": page,
                "_size": 50,
                "_fetch_banner": 0,
                "_fetch_share": 0,
                "_fetch_total": 1,
                "_sort": "file_type:asc,updated_at:desc",
            })
            d = data.get("data") or {}
            items = d.get("list") or []
            for x in items:
                out.append(RemoteFile(
                    fid=x["fid"],
                    name=x["file_name"],
                    is_dir=bool(x.get("dir")),
                    size=int(x.get("size") or 0),
                    parent_fid=pdir_fid,
                    fid_token=x.get("share_fid_token"),
                ))
            meta = data.get("metadata") or {}
            total = meta.get("_total") or d.get("total") or 0
            if page * 50 >= total or not items:
                break
            page += 1
        return out

    def list_share_recursive(
        self,
        pwd_id: str,
        stoken: str,
        pdir_fid: str = "0",
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
        """把分享里的若干 fid 存到自己网盘 to_pdir_fid,返回新 fid 列表。"""
        data = self._post("/share/sharepage/save", {
            "fid_list": fid_list,
            "fid_token_list": fid_token_list,
            "to_pdir_fid": to_pdir_fid,
            "pwd_id": pwd_id,
            "stoken": stoken,
            "pdir_fid": "0",
            "scene": "link",
        })
        task_id = (data.get("data") or {}).get("task_id")
        if not task_id:
            raise QuarkError(f"save_share 未返回 task_id: {data.get('message')}")
        return self._wait_task(task_id)

    def _wait_task(self, task_id: str, max_wait: float = 60.0) -> list[str]:
        deadline = time.time() + max_wait
        retry = 0
        while time.time() < deadline:
            data = self._get("/task", {"task_id": task_id, "retry_index": retry})
            d = data.get("data") or {}
            status = d.get("status")
            # 2 = success, 1 = running, 0 = pending, other = error
            if status == 2:
                save_as = d.get("save_as") or {}
                fids = save_as.get("save_as_top_fids") or []
                return list(fids)
            if status not in (0, 1):
                raise QuarkError(f"task 失败: {data.get('message')} (status={status})")
            retry += 1
            time.sleep(1.0)
        raise QuarkError("task 超时")

    # -------------------- 自家文件 --------------------
    def list_dir(self, pdir_fid: str) -> list[RemoteFile]:
        out: list[RemoteFile] = []
        page = 1
        while True:
            data = self._get("/file/sort", {
                "pdir_fid": pdir_fid,
                "_page": page,
                "_size": 50,
                "_fetch_total": 1,
                "_fetch_sub_dirs": 0,
                "_sort": "file_type:asc,updated_at:desc",
            })
            d = data.get("data") or {}
            items = d.get("list") or []
            for x in items:
                out.append(RemoteFile(
                    fid=x["fid"],
                    name=x["file_name"],
                    is_dir=bool(x.get("dir")),
                    size=int(x.get("size") or 0),
                    parent_fid=pdir_fid,
                ))
            meta = data.get("metadata") or {}
            total = meta.get("_total") or d.get("total") or 0
            if page * 50 >= total or not items:
                break
            page += 1
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
        data = self._post("/file", {
            "pdir_fid": parent_fid,
            "file_name": name,
            "dir_path": "",
            "dir_init_lock": False,
        })
        d = data.get("data") or {}
        fid = d.get("fid")
        if not fid:
            raise QuarkError(f"mkdir 失败: {data.get('message')}")
        return fid

    def mkdir_p(self, path: str) -> str:
        """path 以 / 开头,不存在就创建,返回末端 fid。"""
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
        """只查不建。返回 fid 或 None。"""
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
        self._post("/file/rename", {"fid": fid, "file_name": new_name})

    def move(self, fids: Iterable[str], to_pdir_fid: str) -> None:
        fids = list(fids)
        if not fids:
            return
        data = self._post("/file/move", {
            "action_type": 1,
            "filelist": fids,
            "to_pdir_fid": to_pdir_fid,
            "exclude_fids": [],
        })
        task_id = (data.get("data") or {}).get("task_id")
        if task_id:
            self._wait_task(task_id, max_wait=120)

    # -------------------- 小文件上传 --------------------
    def upload_bytes(
        self,
        parent_fid: str,
        name: str,
        data: bytes,
        mime: str = "application/octet-stream",
    ) -> Optional[str]:
        """上传 bytes 为 parent_fid 下名为 name 的文件。返回新 fid 或 None。
        流程:pre → hash(秒传尝试) → upload/auth + OSS PUT + CompleteMultipart → finish
        """
        size = len(data)
        md5 = hashlib.md5(data).hexdigest()
        sha1 = hashlib.sha1(data).hexdigest()

        now_ms = int(time.time() * 1000)
        pre = self._post("/file/upload/pre", {
            "ccp_hash_update": True,
            "dir_name": "",
            "file_name": name,
            "format_type": mime,
            "l_created_at": now_ms,
            "l_updated_at": now_ms,
            "pdir_fid": parent_fid,
            "parallel_upload": True,
            "size": size,
        })
        d = pre.get("data") or {}
        if d.get("finish"):
            return d.get("fid")

        task_id = d["task_id"]
        fid = d.get("fid")
        bucket = d["bucket"]
        obj_key = d["obj_key"]
        upload_id = d["upload_id"]
        # upload_url 形如 "http://pds.quark.cn";真正 OSS 端点是
        # https://{bucket}.pds.quark.cn(CNAME 到 ccp-sz3-sz-*.oss.aliyuncs.com)
        upload_host = d["upload_url"].split("://", 1)[-1].rstrip("/")
        upload_base = f"https://{bucket}.{upload_host}"
        auth_info = d["auth_info"]
        callback = d.get("callback") or {}
        # OSS callback 要 base64(JSON) 作为 x-oss-callback 头,签名里也要带
        callback_b64 = ""
        if callback:
            cb_obj = {
                "callbackUrl": callback.get("callbackUrl", ""),
                "callbackBody": callback.get("callbackBody", ""),
                "callbackBodyType": "application/x-www-form-urlencoded",
            }
            callback_b64 = base64.b64encode(
                json.dumps(cb_obj, separators=(",", ":")).encode("utf-8")
            ).decode("ascii")

        # hash 秒传尝试
        hash_resp = self._post("/file/update/hash", {
            "md5": md5,
            "sha1": sha1,
            "task_id": task_id,
        })
        if (hash_resp.get("data") or {}).get("finish"):
            return fid

        # 上传第 1 片
        date_gmt = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime())
        auth_meta_put = (
            f"PUT\n\n{mime}\n{date_gmt}\n"
            f"/{bucket}/{obj_key}?partNumber=1&uploadId={upload_id}"
        )
        auth_key = self._upload_auth(task_id, auth_info, auth_meta_put)

        part_url = f"{upload_base}/{obj_key}?partNumber=1&uploadId={upload_id}"
        put_resp = requests.put(
            part_url,
            data=data,
            headers={
                "Authorization": auth_key,
                "Content-Type": mime,
                "Date": date_gmt,
            },
            timeout=60,
        )
        if put_resp.status_code >= 400:
            raise QuarkError(
                f"OSS PUT 失败 {put_resp.status_code}: {put_resp.text[:300]}"
            )
        etag = put_resp.headers.get("ETag") or put_resp.headers.get("etag") or ""
        if not etag:
            raise QuarkError("OSS PUT 没返回 ETag")

        # Complete multipart upload
        date_gmt2 = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime())
        body_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            "<CompleteMultipartUpload>\n"
            f"<Part><PartNumber>1</PartNumber><ETag>{etag}</ETag></Part>\n"
            "</CompleteMultipartUpload>"
        )
        # OSS canonicalized headers: 字典序,一行一个
        canonical_headers = ""
        if callback_b64:
            canonical_headers = f"x-oss-callback:{callback_b64}\n"
        auth_meta_cmp = (
            f"POST\n\napplication/xml\n{date_gmt2}\n"
            f"{canonical_headers}"
            f"/{bucket}/{obj_key}?uploadId={upload_id}"
        )
        auth_key2 = self._upload_auth(task_id, auth_info, auth_meta_cmp)
        cmp_url = f"{upload_base}/{obj_key}?uploadId={upload_id}"
        cmp_headers = {
            "Authorization": auth_key2,
            "Content-Type": "application/xml",
            "Date": date_gmt2,
        }
        if callback_b64:
            cmp_headers["x-oss-callback"] = callback_b64
        cmp_resp = requests.post(
            cmp_url,
            data=body_xml.encode("utf-8"),
            headers=cmp_headers,
            timeout=30,
        )
        if cmp_resp.status_code >= 400:
            raise QuarkError(
                f"CompleteMultipartUpload 失败 {cmp_resp.status_code}: {cmp_resp.text[:300]}"
            )

        # 收尾
        self._post("/file/upload/finish", {"obj_key": obj_key, "task_id": task_id})
        return fid

    def _upload_auth(self, task_id: str, auth_info: str, auth_meta: str) -> str:
        data = self._post("/file/upload/auth", {
            "task_id": task_id,
            "auth_info": auth_info,
            "auth_meta": auth_meta,
        })
        key = (data.get("data") or {}).get("auth_key")
        if not key:
            raise QuarkError(f"upload/auth 没返回 auth_key: {data.get('message')}")
        return key

    def upload_bytes_to_path(
        self,
        path: str,
        data: bytes,
        mime: str = "application/octet-stream",
    ) -> Optional[str]:
        """/abs/path/xxx.nfo 形式,自动 mkdir_p 父目录。"""
        path = path.strip()
        if "/" not in path:
            raise QuarkError(f"path 必须以 / 开头: {path}")
        parent, name = path.rsplit("/", 1)
        parent_fid = self.mkdir_p(parent or "/")
        return self.upload_bytes(parent_fid, name, data, mime)

    def delete(self, fids: Iterable[str]) -> None:
        fids = list(fids)
        if not fids:
            return
        data = self._post("/file/delete", {
            "action_type": 2,
            "filelist": fids,
            "exclude_fids": [],
        })
        task_id = (data.get("data") or {}).get("task_id")
        if task_id:
            self._wait_task(task_id, max_wait=120)
