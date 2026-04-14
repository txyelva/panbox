"""百度网盘适配器。

认证走 cookie(网页登录后 F12 拿 BDUSS / STOKEN / BAIDUID 等):
  Cookie: BDUSS=xxx; STOKEN=xxx; BAIDUID=xxx

分享链接格式:
  https://pan.baidu.com/s/1SHARE_CODE
  https://pan.baidu.com/s/1SHARE_CODE?pwd=PASSCODE

stoken 约定: JSON 字符串,包含 {shareid, uk, sekey, shorturl}。
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.parse
from typing import Iterable, Optional

import requests

from .base import Cloud, CloudError, RemoteFile

API = "https://pan.baidu.com"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def parse_share_url(url: str) -> tuple[str, Optional[str]]:
    m = re.search(r"pan\.baidu\.com/s/([A-Za-z0-9_-]+)", url)
    if not m:
        raise CloudError(f"不是有效的百度网盘分享链接: {url}")
    shorturl = m.group(1)
    if shorturl.startswith("1"):
        shorturl = shorturl[1:]
    pw: Optional[str] = None
    m2 = re.search(r"[?&]pwd=([0-9a-zA-Z]+)", url)
    if m2:
        pw = m2.group(1)
    return shorturl, pw


class BaiduClient(Cloud):
    name = "baidu"

    def __init__(self, cookie: str, request_interval: float = 0.3):
        if not cookie:
            raise CloudError("百度网盘 cookie 为空")
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": UA,
            "Referer": "https://pan.baidu.com/disk/home",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
        })
        # 把原始 cookie 字符串解析到 jar,让后续 Set-Cookie 能自然合并
        for part in cookie.split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            name, value = part.split("=", 1)
            self.session.cookies.set(name, value, domain=".baidu.com")
        self.interval = request_interval
        self._bdstoken: Optional[str] = None
        self._path_cache: dict[str, str] = {"/": "/", "": "/"}
        self._share_dir_path_map: dict[str, str] = {}
        self._own_fsid_path_map: dict[str, str] = {}

    def _get_bdstoken(self) -> str:
        if self._bdstoken:
            return self._bdstoken
        r = self.session.get(f"{API}/disk/home", timeout=15)
        time.sleep(self.interval)
        m = re.search(r'"bdstoken":"([a-f0-9]{32})"', r.text)
        if m:
            self._bdstoken = m.group(1)
            return self._bdstoken
        r = self.session.get(
            f"{API}/api/gettemplatevariable",
            params={
                "fields": '["bdstoken"]',
                "channel": "chunlei",
                "web": "1",
                "app_id": "250528",
                "clienttype": "0",
            },
            timeout=15,
        )
        time.sleep(self.interval)
        try:
            data = r.json()
        except Exception:
            data = {}
        if isinstance(data, dict) and data.get("errno") == 0:
            self._bdstoken = (data.get("result") or {}).get("bdstoken", "")
        if not self._bdstoken:
            raise CloudError("无法获取 bdstoken,请检查 cookie 是否有效")
        return self._bdstoken

    def _get(self, path: str, params: Optional[dict] = None, **kwargs) -> dict:
        p = dict(params or {})
        p.setdefault("channel", "chunlei")
        p.setdefault("clienttype", "0")
        p.setdefault("web", "1")
        p.setdefault("app_id", "250528")
        if not kwargs.get("no_token"):
            p.setdefault("bdstoken", self._get_bdstoken())
        r = self.session.get(
            f"{API}{path}",
            params=p,
            timeout=20,
        )
        time.sleep(self.interval)
        if r.status_code >= 400:
            raise CloudError(f"GET {path} HTTP {r.status_code}: {r.text[:400]}")
        try:
            data = r.json()
        except Exception:
            data = {}
        if (
            isinstance(data, dict)
            and data.get("errno") not in (0, None)
            and not kwargs.get("ignore_errno")
        ):
            if data.get("errno") not in kwargs.get("ok_errnos", ()):
                raise CloudError(f"GET {path} 失败 errno={data.get('errno')}: {data}")
        return data

    def _post(self, path: str, data: Optional[dict] = None, params: Optional[dict] = None, **kwargs) -> dict:
        p = dict(params or {})
        if not kwargs.get("no_token"):
            p["bdstoken"] = self._get_bdstoken()
        p.setdefault("channel", "chunlei")
        p.setdefault("clienttype", "0")
        p.setdefault("web", "1")
        p.setdefault("app_id", "250528")
        body = dict(data or {})
        r = self.session.post(
            f"{API}{path}",
            params=p,
            data=body,
            timeout=20,
        )
        time.sleep(self.interval)
        if r.status_code >= 400:
            raise CloudError(f"POST {path} HTTP {r.status_code}: {r.text[:400]}")
        try:
            resp = r.json()
        except Exception:
            resp = {}
        if (
            isinstance(resp, dict)
            and resp.get("errno") not in (0, None)
            and not kwargs.get("ignore_errno")
        ):
            if resp.get("errno") not in kwargs.get("ok_errnos", ()):
                raise CloudError(f"POST {path} 失败 errno={resp.get('errno')}: {resp}")
        return resp

    def _fetch_shareinfo(self, shorturl: str, passcode: str = "") -> dict:
        try:
            resp = self.session.post(
                f"{API}/share/wxlist",
                params={
                    "shorturl": shorturl,
                    "pwd": passcode or "",
                    "page": "1",
                    "num": "1",
                },
                timeout=15,
            )
            time.sleep(self.interval)
            data = resp.json()
            if data.get("errno") == 0:
                shareinfo = (data.get("data") or {}).get("shareinfo") or {}
                sid = shareinfo.get("shareid")
                uk = shareinfo.get("uk")
                if sid and uk and str(uk) not in ("", "0"):
                    return {"shareid": str(sid), "uk": str(uk)}
        except Exception:
            pass
        return {}

    @staticmethod
    def _unpack_stoken(stoken: str) -> dict:
        return json.loads(stoken)

    def _to_remote_file_share(self, x: dict, parent_path: str = "/") -> RemoteFile:
        is_dir = bool(x.get("isdir"))
        fid = str(x.get("fs_id") or "")
        name = x.get("server_filename") or x.get("filename") or ""
        path = x.get("path", "")
        if is_dir and path:
            self._share_dir_path_map[fid] = path
        return RemoteFile(
            fid=fid,
            name=name,
            is_dir=is_dir,
            size=int(x.get("size") or 0),
            parent_fid=parent_path,
            fid_token=None,
        )

    def _to_remote_file_own(self, x: dict, parent_path: str = "/") -> RemoteFile:
        is_dir = bool(x.get("isdir"))
        path = x.get("path", "")
        if is_dir:
            # 百度自家盘用 path 做目录标识最稳,避免 numeric fs_id 被当成 dir 参数
            fid = path or str(x.get("fs_id") or "")
        else:
            fid = str(x.get("fs_id") or "")
        name = x.get("server_filename") or ""
        if path:
            self._own_fsid_path_map[fid] = path
        return RemoteFile(
            fid=fid,
            name=name,
            is_dir=is_dir,
            size=int(x.get("size") or 0),
            parent_fid=parent_path,
        )

    def _path_by_fid(self, fid: str) -> str:
        if fid in self._own_fsid_path_map:
            return self._own_fsid_path_map[fid]
        raise CloudError(f"无法找到 fid={fid} 对应的路径")

    # -------------------- 分享 --------------------
    def get_stoken(self, pwd_id: str, passcode: str = "") -> str:
        shorturl = pwd_id
        shareid = ""
        uk = ""
        sekey = ""

        info = self._fetch_shareinfo(shorturl, passcode)
        shareid = info.get("shareid", "")
        uk = info.get("uk", "")

        if not shareid or not uk:
            r = self.session.get(f"{API}/s/1{shorturl}", timeout=15)
            time.sleep(self.interval)
            html = r.text

            for pat in [r'"shareid":\s*"?(\d+)"?', r'"share_id":\s*"?(\d+)"?']:
                m = re.search(pat, html)
                if m:
                    shareid = m.group(1)
                    break
            for pat in [r'"share_uk":\s*"?(\d+)"?', r'"uk":\s*"?(\d+)"?']:
                m = re.search(pat, html)
                if m:
                    val = m.group(1)
                    if val and val != "0":
                        uk = val
                        break

            if not shareid or not uk:
                m = re.search(r'yunData\.setData\(({.+?})\)', html, re.DOTALL)
                if m:
                    try:
                        yd = json.loads(m.group(1))
                        shareid = str(yd.get("shareid") or yd.get("share_id") or "")
                        uk = str(yd.get("uk") or yd.get("share_uk") or "")
                    except Exception:
                        pass

        if not shareid or not uk:
            raise CloudError("无法获取分享信息(shareid/uk),可能需要密码或分享已失效")

        if passcode and not sekey:
            resp = self._post(
                "/share/verify",
                data={"pwd": passcode, "vcode": "", "vcode_str": ""},
                params={"surl": shorturl},
                no_token=True,
                ok_errnos=(0,),
            )
            sekey = resp.get("randsk", "")
            if sekey:
                # randsk 通常是 url-encoded 的;存原始值并在请求时正确编码
                sekey = urllib.parse.unquote(sekey)
                self.session.cookies.set("BDCLND", urllib.parse.quote(sekey), domain=".baidu.com")

        return json.dumps(
            {"shareid": shareid, "uk": uk, "sekey": sekey, "shorturl": shorturl},
            separators=(",", ":"),
        )

    @staticmethod
    def _to_baidu_path(pdir_fid: str) -> str:
        """pipeline 统一用 '0' 表示根目录,百度用 '' 或 '/' 表示根目录。"""
        if pdir_fid in ("0", "", "/"):
            return ""
        return pdir_fid

    def list_share(
        self, pwd_id: str, stoken: str, pdir_fid: str = "0"
    ) -> list[RemoteFile]:
        info = self._unpack_stoken(stoken)
        shareid = info["shareid"]
        uk = info["uk"]
        sekey = info.get("sekey", "")
        shorturl = info.get("shorturl", pwd_id)

        dir_path = self._to_baidu_path(pdir_fid)
        params: dict[str, str] = {
            "shareid": shareid,
            "uk": uk,
            "root": "1" if not dir_path else "0",
            "page": "1",
            "num": "1000",
            "shorturl": shorturl,
            "dir": dir_path,
            "order": "time",
            "desc": "1",
            "_": str(int(time.time() * 1000)),
        }
        if sekey:
            # requests 会自动对 params 做 URL encode,这里传 raw 即可
            params["sekey"] = sekey

        data = self._get("/share/list", params=params, no_token=True)
        items = data.get("list") or []
        return [self._to_remote_file_share(x, parent_path=dir_path or "/") for x in items]

    def list_share_recursive(
        self, pwd_id: str, stoken: str, pdir_fid: str = "/"
    ) -> list[RemoteFile]:
        out: list[RemoteFile] = []
        stack = [pdir_fid]
        while stack:
            cur = stack.pop()
            items = self.list_share(pwd_id, stoken, cur)
            for f in items:
                out.append(f)
                if f.is_dir:
                    dir_path = self._share_dir_path_map.get(f.fid)
                    if dir_path:
                        stack.append(dir_path)
        return out

    def save_share(
        self,
        pwd_id: str,
        stoken: str,
        fid_list: list[str],
        fid_token_list: list[str],
        to_pdir_fid: str,
    ) -> list[str]:
        info = self._unpack_stoken(stoken)
        shareid = info["shareid"]
        uk = info["uk"]
        sekey = info.get("sekey", "")

        target_path = to_pdir_fid if to_pdir_fid not in ("", "/") else "/"
        fsids = [int(fid) for fid in fid_list if fid]

        params: dict[str, str] = {
            "shareid": shareid,
            "from": uk,
            "ondup": "newcopy",
            "async": "1",
        }
        if sekey:
            # requests 会自动对 params 做 URL encode,这里传 raw 即可
            params["sekey"] = sekey

        body = {
            "fsidlist": json.dumps(fsids),
            "path": target_path,
        }
        self._post("/share/transfer", data=body, params=params)
        return []

    # -------------------- 自家盘 --------------------
    def list_dir(self, pdir_fid: str) -> list[RemoteFile]:
        path = pdir_fid if pdir_fid not in ("", "/") else "/"
        out: list[RemoteFile] = []
        page = 1
        while True:
            params = {
                "dir": path,
                "order": "time",
                "desc": "1",
                "showempty": "0",
                "page": str(page),
                "num": "1000",
            }
            data = self._get("/api/list", params=params)
            items = data.get("list") or []
            for x in items:
                out.append(self._to_remote_file_own(x, parent_path=path))
            if len(items) < 1000:
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
                    dir_path = self._own_fsid_path_map.get(f.fid)
                    if dir_path:
                        stack.append(dir_path)
        return out

    def mkdir(self, parent_fid: str, name: str) -> str:
        parent_path = parent_fid if parent_fid not in ("", "/") else "/"
        new_path = f"{parent_path.rstrip('/')}/{name}"
        self._post(
            "/api/create",
            data={
                "path": new_path,
                "isdir": "1",
                "size": "0",
                "block_list": "[]",
                "method": "post",
            },
            params={"a": "commit"},
        )
        for f in self.list_dir(parent_path):
            if f.is_dir and f.name == name:
                return f.fid
        raise CloudError(f"mkdir 后未找到目录: {new_path}")

    def mkdir_p(self, path: str) -> str:
        path = path.strip()
        if not path or path == "/":
            return "/"
        if path in self._path_cache:
            return self._path_cache[path]
        segs = [s for s in path.split("/") if s]
        cur_fid = "/"
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
            return "/"
        if path in self._path_cache:
            return self._path_cache[path]
        segs = [s for s in path.split("/") if s]
        cur_fid = "/"
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
        path = self._path_by_fid(fid)
        self._post(
            "/api/filemanager",
            data={
                "filelist": json.dumps([{"path": path, "newname": new_name}]),
            },
            params={"opera": "rename", "async": "2"},
        )
        # 更新缓存为新路径
        parent = path.rsplit("/", 1)[0]
        new_path = f"{parent}/{new_name}"
        self._own_fsid_path_map[fid] = new_path

    def move(self, fids: Iterable[str], to_pdir_fid: str) -> None:
        fids = list(fids)
        if not fids:
            return
        dest_path = to_pdir_fid if to_pdir_fid not in ("", "/") else "/"
        filelist = []
        for fid in fids:
            path = self._path_by_fid(fid)
            name = path.split("/")[-1]
            filelist.append({"path": path, "dest": dest_path, "newname": name})
        self._post(
            "/api/filemanager",
            data={
                "filelist": json.dumps(filelist),
            },
            params={"opera": "move", "async": "2"},
        )
        for fid in fids:
            self._own_fsid_path_map.pop(fid, None)

    def delete(self, fids: Iterable[str]) -> None:
        fids = list(fids)
        if not fids:
            return
        filelist = []
        for fid in fids:
            path = self._path_by_fid(fid)
            filelist.append({"path": path})
        self._post(
            "/api/filemanager",
            data={
                "filelist": json.dumps(filelist),
            },
            params={"opera": "delete", "async": "2"},
        )
        for fid in fids:
            self._own_fsid_path_map.pop(fid, None)

    # -------------------- 小文件上传 --------------------
    def upload_bytes(
        self,
        parent_fid: str,
        name: str,
        data: bytes,
        mime: str = "application/octet-stream",
    ) -> Optional[str]:
        parent_path = parent_fid if parent_fid not in ("", "/") else "/"
        file_path = f"{parent_path.rstrip('/')}/{name}"
        size = len(data)
        md5 = hashlib.md5(data).hexdigest()

        pre = self._post(
            "/api/precreate",
            data={
                "path": file_path,
                "isdir": "0",
                "autoinit": "1",
                "size": str(size),
                "block_list": json.dumps([md5]),
                "rtype": "3",
            },
        )

        pre_data = pre.get("data") or pre
        uploadid = pre_data.get("uploadid")
        return_type = pre_data.get("return_type")

        if return_type == 2:
            # 秒传成功,百度已有该 block,无需上传也无需 create
            return None
        elif return_type == 1 and uploadid:
            upload_url = (
                f"https://d.pcs.baidu.com/rest/2.0/pcs/superfile2"
                f"?method=upload&path={urllib.parse.quote(file_path)}"
                f"&uploadid={urllib.parse.quote(str(uploadid))}&partseq=0"
                f"&app_id=250528"
            )
            up = self.session.post(
                upload_url,
                files={"file": (name, data, mime)},
                timeout=60,
            )
            time.sleep(self.interval)
            if up.status_code >= 400:
                raise CloudError(f"百度上传失败 {up.status_code}: {up.text[:300]}")
        else:
            raise CloudError(f"precreate 异常: {pre}")

        self._post(
            "/api/create",
            data={
                "path": file_path,
                "isdir": "0",
                "size": str(size),
                "uploadid": uploadid or "",
                "block_list": json.dumps([md5]),
                "rtype": "3",
            },
            params={"a": "commit"},
        )

        for f in self.list_dir(parent_path):
            if f.name == name:
                return f.fid
        return None
