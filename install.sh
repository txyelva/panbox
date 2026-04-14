#!/usr/bin/env bash
# panbox 一键安装脚本
# 用法: bash <(curl -fsSL https://raw.githubusercontent.com/txyelva/panbox/main/install.sh)
#   或: bash install.sh        (已 clone 仓库后本地运行)

set -e

REPO="https://github.com/txyelva/panbox.git"
INSTALL_DIR="$HOME/.local/panbox"
BIN_DIR="$HOME/.local/bin"

# ── 颜色 ──────────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BOLD='\033[1m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✓${NC} $*"; }
warn() { echo -e "${YELLOW}!${NC} $*"; }
die()  { echo -e "${RED}✗ $*${NC}"; exit 1; }

echo -e "${BOLD}panbox 安装程序${NC}"
echo "────────────────────────────────────"

# ── 1. 检查 Python ────────────────────────────────────────────────────────────
PYTHON=""
for cmd in python3 python; do
    if command -v "$cmd" &>/dev/null; then
        ver=$("$cmd" -c 'import sys; print(sys.version_info >= (3,9))' 2>/dev/null)
        if [ "$ver" = "True" ]; then
            PYTHON="$cmd"
            break
        fi
    fi
done
[ -z "$PYTHON" ] && die "需要 Python 3.9 或更高版本。请先安装 Python: https://python.org"
ok "Python: $($PYTHON --version)"

# ── 2. 下载 / 更新仓库 ────────────────────────────────────────────────────────
if [ -d "$INSTALL_DIR/.git" ]; then
    echo "检测到已有安装,更新中..."
    git -C "$INSTALL_DIR" pull --ff-only
    ok "代码已更新"
else
    echo "下载 panbox..."
    git clone --depth=1 "$REPO" "$INSTALL_DIR"
    ok "代码已下载到 $INSTALL_DIR"
fi

# ── 3. 创建虚拟环境并安装 ─────────────────────────────────────────────────────
cd "$INSTALL_DIR"
if [ ! -d ".venv" ]; then
    $PYTHON -m venv .venv
fi
source .venv/bin/activate
pip install -q -e .
ok "panbox 已安装"

# ── 4. 写入 PATH 包装脚本 ─────────────────────────────────────────────────────
mkdir -p "$BIN_DIR"
cat > "$BIN_DIR/panbox" <<EOF
#!/usr/bin/env bash
exec "$INSTALL_DIR/.venv/bin/panbox" "\$@"
EOF
chmod +x "$BIN_DIR/panbox"
ok "命令包装脚本写入 $BIN_DIR/panbox"

# 把 ~/.local/bin 加到 PATH(如果还没有)
SHELL_RC=""
case "$SHELL" in
    */zsh)  SHELL_RC="$HOME/.zshrc" ;;
    */bash) SHELL_RC="$HOME/.bashrc" ;;
esac
if [ -n "$SHELL_RC" ]; then
    if ! grep -q "$BIN_DIR" "$SHELL_RC" 2>/dev/null; then
        echo "export PATH=\"$BIN_DIR:\$PATH\"" >> "$SHELL_RC"
        warn "已写入 $SHELL_RC,请执行: source $SHELL_RC  (或重开终端)"
    fi
fi

# ── 5. 初始化配置 ─────────────────────────────────────────────────────────────
export PATH="$BIN_DIR:$PATH"
if [ ! -f "$HOME/.config/panbox/config.yaml" ]; then
    panbox config init
    ok "配置文件已生成: ~/.config/panbox/config.yaml"
    echo ""
    warn "下一步:编辑配置文件,填入 TMDB API Key 和各云盘凭据"
    warn "  nano ~/.config/panbox/config.yaml"
else
    ok "配置文件已存在,跳过"
fi

# ── 6. 安装 Skill(检测已安装的 Agent) ────────────────────────────────────────
echo ""
echo "安装 Agent Skill..."
SKILL_SRC="$INSTALL_DIR/skills/panbox/SKILL.md"
INSTALLED_AGENTS=()

install_skill() {
    local agent_name="$1"
    local skill_dir="$2"
    mkdir -p "$skill_dir"
    cp "$SKILL_SRC" "$skill_dir/SKILL.md"
    ok "Skill 已安装 → $agent_name ($skill_dir)"
    INSTALLED_AGENTS+=("$agent_name")
}

# Claude Code
[ -d "$HOME/.claude" ] && install_skill "Claude Code" "$HOME/.claude/skills/panbox"

# OpenClaw
[ -d "$HOME/.openclaw" ] && install_skill "OpenClaw" "$HOME/.openclaw/skills/panbox"

# Hermes
[ -d "$HOME/.hermes" ] && install_skill "Hermes" "$HOME/.hermes/skills/panbox"

if [ ${#INSTALLED_AGENTS[@]} -eq 0 ]; then
    warn "未检测到已安装的 Agent。手动安装 Skill:"
    echo "  Claude Code : mkdir -p ~/.claude/skills/panbox && cp $SKILL_SRC ~/.claude/skills/panbox/"
    echo "  OpenClaw    : mkdir -p ~/.openclaw/skills/panbox && cp $SKILL_SRC ~/.openclaw/skills/panbox/"
    echo "  Hermes      : mkdir -p ~/.hermes/skills/panbox && cp $SKILL_SRC ~/.hermes/skills/panbox/"
fi

# ── 7. 完成 ───────────────────────────────────────────────────────────────────
echo ""
echo "────────────────────────────────────"
echo -e "${BOLD}安装完成!${NC}"
echo ""
echo "下一步:"
echo "  1. 编辑配置:  nano ~/.config/panbox/config.yaml"
echo "  2. 验证配置:  panbox doctor"
echo "  3. 测试入库:  panbox ingest <分享链接> --hint '剧名' --dry-run"
echo ""
echo "文档: https://github.com/txyelva/panbox"
