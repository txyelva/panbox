---
name: panbox
description: 网盘入库 — 把夸克/阿里云盘/115网盘/百度网盘分享链接自动转存、TMDB 识别、按 Plex/Emby 布局重命名落库,每集自动生成 NFO + 缩略图。遇到已有剧集只补新集。触发词:pan.quark.cn、alipan.com、aliyundrive.com、115.com、115cdn.com、pan.baidu.com、夸克分享、阿里云盘、115分享、百度分享、入库、刮削、转存归档。
---

# panbox — 网盘入库

一条命令完成「转存 → 识别 → 落库 → 刮削」:`panbox ingest <URL> --hint "<准确名>" --json`。根据 URL 自动选云盘(夸克 / 阿里云盘 / 115 / 百度网盘),落库后自动写 `tvshow.nfo` / `movie.nfo`、poster/fanart、每集 `episodedetails` NFO 和缩略图(TMDB 有图时)。

## 何时触发

满足任一条件就用这个 skill:

- 用户消息里包含以下任一链接:
  - 夸克:`https://pan.quark.cn/s/...`
  - 阿里云盘:`https://www.alipan.com/s/...` 或 `https://www.aliyundrive.com/s/...`
  - 115:`https://115.com/s/...` 或 `https://115cdn.com/s/...`
  - 百度网盘:`https://pan.baidu.com/s/...`
- 用户明确说「帮我入库」「刮削一下」「转存并归档」「这个剧加到库里」之类
- 用户说「看看这个链接」并带网盘链接 → 通常也是入库意图,先问一句确认

**不适用**:单纯问链接内容是什么、或只想本地下载。

## CLI

```bash
/Users/ttpg/Documents/Claude/kuakeauto/panbox/.venv/bin/panbox ingest <URL> \
    [--hint "准确剧名"] \
    [--type movie|tv] \
    [--passcode XXXX] \
    [--yes] \
    [--dry-run] \
    --json
```

**必须加 `--json`**,拿结构化结果再向用户汇报。不要解析人类输出。

返回 JSON 字段:

| 字段 | 含义 |
|---|---|
| `status` | `ok` / `need_confirm` / `skipped` / `error` |
| `type` | `movie` 或 `tv` |
| `tmdb_id` | TMDB ID |
| `title` / `year` | TMDB 标准化标题和年份 |
| `path` | 最终落地的目录路径 |
| `added` | 本次入库的文件名列表 |
| `skipped` | 被跳过(库里已有或解析失败)的文件 |
| `candidates` | `status=need_confirm` 时的候选 TMDB 结果,含 `tmdb_id/title/year/type/overview` |
| `message` | 补充说明或错误信息 |

## 标准流程

### 1. 提取 URL 和 hint

从用户消息里拿两样东西:

- **URL**:直接拿原始链接,含密码参数一起传(工具自动提取)
  - 夸克:`https://pan.quark.cn/s/XXXXXX?pwd=yyyy`
  - 阿里:`https://www.alipan.com/s/XXXXXX`
  - 115:`https://115cdn.com/s/XXXXXX?password=yyyy`(注意是 `password=`,不是 `pwd=`)
  - 百度:`https://pan.baidu.com/s/XXXXXX?pwd=yyyy`
- **hint**:用户通常会把准确名称写在链接旁边。例:
  - `https://pan.quark.cn/s/abc 凡人修仙传 第二季 4K` → hint = `"凡人修仙传 第二季 4K"`
  - `https://115cdn.com/s/xyz?password=t58d 蜜语纪.2026` → hint = `"蜜语纪 2026"`
  - `https://www.alipan.com/s/abc 冰湖重生(2026）4K 更新至15集` → hint = `"冰湖重生 (2026)"`

**hint 很关键** — 分享者为避和谐常故意改成无意义名字,文件名不可靠。用户在链接边上写的准确名必须作为权威输入。

hint 里可以保留季度、年份、画质 tag(S01E01、(2026)、4K、WEB-DL),工具会自动清理。

如果用户没给准确名,不要瞎猜 — 先跑 dry-run 看识别结果,识别不对再回问用户。

### 2. 先 dry-run

第一次**必须**先跑 `--dry-run`,只识别不写入:

```bash
panbox ingest <URL> --hint "<hint>" --yes --dry-run --json
```

### 3. 根据结果分支

**`status: ok`**:给用户展示识别结果,问一句确认:
```
识别为:{title} ({year})  [{type}]
目标路径:{path}
要入库吗?
```

**`status: need_confirm`**:把 `candidates` 展示成编号列表,让用户选哪个。选好后把该候选的 `title ({year})` 作为新 hint 重跑(可加 `--yes` 跳过二次候选)。

**`status: error`**:展示 `message`,常见原因:
- 分享里没视频 → 链接失效或是压缩包
- TMDB 未找到 → hint 不准,请用户补英文名或年份
- 链接不合法 → 检查 URL 格式

### 4. 真的执行

用户确认后,**同一条命令去掉 `--dry-run`** 再跑一次:

```bash
panbox ingest <URL> --hint "<同样的 hint>" --yes --json
```

### 5. 报告结果

从返回的 JSON 里总结给用户:

```
✅ 已入库:{title} ({year})
目标:{path}
新增 {len(added)} 集/个文件
跳过 {len(skipped)} 项(库里已有)
```

如果 `added` 是空且 `status=skipped` → 说明库里已经有完整版了,如实告知。

## 各云盘注意事项

| 云盘 | URL 格式 | 密码参数 | 凭据 |
|---|---|---|---|
| 夸克 | `pan.quark.cn/s/XXX` | `?pwd=XXXX` | cookie |
| 阿里云盘 | `alipan.com/s/XXX` 或 `aliyundrive.com/s/XXX` | 无密码 | refresh_token |
| 115 | `115.com/s/XXX` 或 `115cdn.com/s/XXX` | `?password=XXXX` | cookie(UID/CID/SEID) |
| 百度网盘 | `pan.baidu.com/s/XXX` | `?pwd=XXXX` | cookie(BDUSS/BAIDUID/STOKEN) |

**115 特殊行为**:
- 同一分享链接转存两次会报"文件已接收"(幂等,不报错继续)
- `save_share` 不返回新 fid,工具用快照差定位新文件,首次 copy 后有短暂延迟
- 文件名常为裸集数(`01 4K.mp4`),工具会用父目录名推断 season

## 断链恢复

- dry-run 成功但用户犹豫很久再确认 → 真跑如报错再 dry-run 验证
- 不要同一链接连续 dry-run 多次 — 浪费 API 配额

## 绝对不要做的事

- **不要自己解析链接内容** — 这是 `panbox ingest` 的职责
- **不要跳过 `--dry-run`** 去执行头一次调用,除非用户明确说"直接入库"
- **不要绕开 hint** — 用户给了就一定要传进去
- **不要把 cookie、TMDB key 暴露在输出里**(它们在 `~/.config/panbox/config.yaml`,`chmod 600`)

## 配置

配置文件 `~/.config/panbox/config.yaml`:

```yaml
tmdb:
  api_key: "..."
  language: zh-CN
clouds:
  quark:
    cookie: "..."           # 夸克网页登录后 F12 复制
    staging_movies: /影视剧/待刮削/待刮削电影
    staging_tv:     /影视剧/待刮削/待刮削电视剧
    library_movies: /影视剧/Movies
    library_tv:     /影视剧/TV
  ali:
    refresh_token: "..."    # alipan.com 登录后本地存储里拿
    staging_movies: /影视剧/待刮削/待刮削电影
    staging_tv:     /影视剧/待刮削/待刮削电视剧
    library_movies: /影视剧/Movies
    library_tv:     /影视剧/TV
  "115":                    # 注意引号,YAML 中纯数字 key 需加引号
    cookie: "UID=xxx; CID=xxx; SEID=xxx"   # F12 → Application → Cookies
    staging_movies: /影视剧/待刮削/待刮削电影
    staging_tv:     /影视剧/待刮削/待刮削电视剧
    library_movies: /影视剧/Movies
    library_tv:     /影视剧/TV
  baidu:
    cookie: "BDUSS=xxx; STOKEN=xxx; BAIDUID=xxx"   # 百度网盘网页登录后 F12 复制 Cookie
    staging_movies: /影视剧/待刮削/待刮削电影
    staging_tv:     /影视剧/待刮削/待刮削电视剧
    library_movies: /影视剧/Movies
    library_tv:     /影视剧/TV
policy:
  on_movie_exists: skip        # skip | replace
  on_tv_incomplete: diff_only  # diff_only 只补新集
  ask_when_ambiguous: true
  write_metadata: true         # 自动写 NFO + poster/fanart + 每集 thumb
```

调试用:
- `panbox doctor` — 检查配置和多云盘连通性
- `panbox identify --name "..."` — 只识别不入库,验证 hint 解析
- `panbox config path` — 打印配置路径
