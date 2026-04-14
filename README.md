# panbox

一句话把网盘分享链接变成 Plex / Emby 规范媒体库。

```
panbox ingest https://pan.quark.cn/s/xxx?pwd=yyyy --hint "凡人修仙传 第二季"
```

**支持云盘**：夸克 · 阿里云盘 · 115网盘 · 百度网盘

**自动完成**：
1. 把分享内容转存到你的网盘暂存目录
2. TMDB 识别标题 / 年份 / 剧集
3. 按 `Title (Year)/Season XX/Title - S01E01.ext` 布局重命名并移入媒体库
4. 写入 `tvshow.nfo` / `movie.nfo`、`poster.jpg`、`fanart.jpg`、每集 `episodedetails` NFO 和缩略图

已有剧集只补缺集，不重复入库。

---

## 架构说明

panbox 由两部分组成，缺一不可：

```
用户 → Agent (Claude Code / OpenClaw / Hermes / ...) 
           ↓  读取 skills/panbox/SKILL.md（告诉 Agent 怎么调用）
       panbox CLI（本地安装，真正发起 API 请求、操作云盘）
           ↓
       ~/.config/panbox/config.yaml（本地凭据，永不上传）
```

**Skill 文件是纯文本提示词**，告诉 Agent 在什么时候调用哪条命令、如何解析结果。  
**panbox CLI 是实际执行者**，负责所有网络请求和文件操作。

> 因此，仅复制 Skill 文件是不够的——本机必须安装 panbox CLI 并填好配置，Skill 才能工作。

---

## 安装

### 一键安装（推荐）

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/txyelva/panbox/main/install.sh)
```

脚本会自动完成：下载代码 → 创建虚拟环境 → 安装依赖 → 生成配置模板 → 检测并安装 Agent Skill（Claude Code / OpenClaw / Hermes）。

### 手动安装

```bash
git clone https://github.com/txyelva/panbox.git
cd panbox
python3 -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e .
panbox config init
```

> Python ≥ 3.9 required

---

## 配置

### 初始化配置文件

```bash
panbox config init
```

生成 `~/.config/panbox/config.yaml`，按下面说明填入凭据。

### TMDB

在 [themoviedb.org/settings/api](https://www.themoviedb.org/settings/api) 申请免费 API Key（v3 auth）。

```yaml
tmdb:
  api_key: "YOUR_TMDB_API_KEY"
  language: zh-CN
```

### 夸克网盘

1. 浏览器登录 [pan.quark.cn](https://pan.quark.cn)
2. F12 → Network → 任意请求 → Request Headers → 复制 `Cookie` 值

```yaml
clouds:
  quark:
    cookie: "QUARK_COOKIE_STRING"
    staging_movies: /影视剧/待刮削/待刮削电影
    staging_tv:     /影视剧/待刮削/待刮削电视剧
    library_movies: /影视剧/Movies
    library_tv:     /影视剧/TV
```

### 阿里云盘

1. 浏览器登录 [alipan.com](https://www.alipan.com)
2. F12 → Application → Local Storage → `token` → 复制 `refresh_token` 字段值

```yaml
  ali:
    refresh_token: "YOUR_REFRESH_TOKEN"
    staging_movies: /待刮削/电影
    staging_tv:     /待刮削/剧集
    library_movies: /影视剧/Movies
    library_tv:     /影视剧/TV
```

### 115网盘

1. 浏览器登录 [115.com](https://115.com)
2. F12 → Application → Cookies → 复制 `UID`、`CID`、`SEID` 拼成字符串

```yaml
  "115":            # 注意引号，YAML 中纯数字 key 需加引号
    cookie: "UID=xxx; CID=xxx; SEID=xxx"
    staging_movies: /待刮削/电影
    staging_tv:     /待刮削/剧集
    library_movies: /影视剧/Movies
    library_tv:     /影视剧/TV
```

### 百度网盘

1. 浏览器登录 [pan.baidu.com](https://pan.baidu.com)
2. F12 → Application → Cookies → 复制完整 Cookie 字符串（至少包含 `BDUSS`、`BAIDUID`、`STOKEN`）

```yaml
  baidu:
    cookie: "BDUSS=xxx; BAIDUID=xxx; STOKEN=xxx"
    staging_movies: /待刮削/电影
    staging_tv:     /待刮削/剧集
    library_movies: /影视剧/Movies
    library_tv:     /影视剧/TV
```

### 策略

```yaml
policy:
  on_movie_exists: skip         # skip | replace
  on_tv_incomplete: diff_only   # 只补缺集
  ask_when_ambiguous: true      # TMDB 多结果时暂停确认
  write_metadata: true          # 写 NFO + 封面 + 每集缩略图
```

---

## 使用

### 入库

```bash
# 基本用法（会先识别，再询问确认）
panbox ingest https://pan.quark.cn/s/xxx?pwd=yyyy --hint "凡人修仙传 第二季"

# 只识别不写入（dry-run）
panbox ingest https://pan.quark.cn/s/xxx --hint "某某剧" --dry-run

# 自动选 TMDB 热度第一（跳过确认）
panbox ingest https://pan.quark.cn/s/xxx --hint "某某剧" --yes

# 指定类型
panbox ingest https://115cdn.com/s/xxx?password=yyyy --hint "某某电影" --type movie
```

**URL 格式与密码参数**：

| 云盘 | URL | 密码参数 |
|------|-----|---------|
| 夸克 | `pan.quark.cn/s/XXX` | `?pwd=XXXX` |
| 阿里云盘 | `alipan.com/s/XXX` 或 `aliyundrive.com/s/XXX` | 无密码 |
| 115 | `115.com/s/XXX` 或 `115cdn.com/s/XXX` | `?password=XXXX` |
| 百度网盘 | `pan.baidu.com/s/XXX` | `?pwd=XXXX` |

### 其他命令

```bash
# 检查配置和各云盘连通性
panbox doctor

# 只查 TMDB 不入库（验证 hint 是否识别正确）
panbox identify --name "凡人修仙传 第二季"
panbox identify --file "FanRen.XiuXian.Zhuan.S02E01.mkv"

# 打印配置文件路径
panbox config path
```

---

## Agent 集成

一键安装脚本会自动检测并安装 Skill。手动安装请参考下表：

| Agent | Skill 目录 |
|-------|-----------|
| [Claude Code](https://claude.ai/code) | `~/.claude/skills/panbox/` |
| [OpenClaw](https://openclaw.ai) | `~/.openclaw/skills/panbox/` |
| Hermes | `~/.hermes/skills/panbox/` |

```bash
# 示例（Claude Code）
mkdir -p ~/.claude/skills/panbox
cp skills/panbox/SKILL.md ~/.claude/skills/panbox/SKILL.md
```

安装后，在任意支持的 Agent 里直接丢链接即可：

```
https://pan.quark.cn/s/abc 凡人修仙传 第二季 4K
```

Agent 会自动走 dry-run → 展示识别结果 → 确认后入库的完整流程。

**兼容性说明**：Skill 文件本质是给 LLM 看的提示词，任何支持 Bash 工具调用的 Agent 均可使用，不限于上表列出的几款。

---

## 目录布局

panbox 生成的目录结构与 Plex / Emby / Jellyfin 兼容：

```
媒体库/TV/
└── 凡人修仙传 (2023)/
    ├── tvshow.nfo
    ├── poster.jpg
    ├── fanart.jpg
    └── Season 01/
        ├── 凡人修仙传 - S01E01.mkv
        ├── 凡人修仙传 - S01E01.nfo
        ├── 凡人修仙传 - S01E01-thumb.jpg
        └── ...

媒体库/Movies/
└── 流浪地球 (2019)/
    ├── 流浪地球 (2019).mkv
    ├── 流浪地球 (2019).nfo
    ├── poster.jpg
    └── fanart.jpg
```

---

## 扩展新云盘

1. 在 `panbox/clouds/` 新建模块，实现 `Cloud` Protocol（参考 `clouds/base.py`）
2. 在 `panbox/clouds/__init__.py` 的 `REGISTRY` 注册 URL 正则和工厂函数
3. 在 `panbox/config.py` 新增对应 Config dataclass
4. 在 `panbox/cli.py` 的 `EXAMPLE_CONFIG` 和凭据校验里补充

---

## License

MIT
