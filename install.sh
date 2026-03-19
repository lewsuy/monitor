#!/bin/bash
# ============================================================
#  系统可用性监控平台 - 一键安装脚本
#  支持: OpenEuler 22.03 / CentOS 7+ / RHEL 8+
#  端口: 8090
# ============================================================
set -e

INSTALL_DIR="/opt/monitor"
SERVICE_USER="root"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

echo "============================================================"
echo "  系统可用性监控平台 安装程序"
echo "============================================================"

# ── 系统检查 ──────────────────────────────────────────────────
if [[ $EUID -ne 0 ]]; then error "请使用 root 用户运行此脚本"; fi

# Python 版本检查
PYTHON=$(command -v python3 || true)
[[ -z "$PYTHON" ]] && error "未找到 python3，请先安装"
PY_VER=$($PYTHON -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
info "Python 版本: $PY_VER"

# ── 安装系统依赖 ───────────────────────────────────────────────
info "安装系统依赖..."
if command -v dnf &>/dev/null; then
    dnf install -y curl unixODBC unixODBC-devel freetds freetds-devel \
        python3-pip gcc python3-devel 2>/dev/null || warn "部分依赖安装失败（非致命）"
elif command -v yum &>/dev/null; then
    yum install -y curl unixODBC unixODBC-devel freetds freetds-devel \
        python3-pip gcc python3-devel 2>/dev/null || warn "部分依赖安装失败（非致命）"
elif command -v apt-get &>/dev/null; then
    apt-get update -qq
    apt-get install -y curl unixodbc unixodbc-dev tdsodbc freetds-bin \
        python3-pip gcc python3-dev 2>/dev/null || warn "部分依赖安装失败（非致命）"
fi

# ── 安装 Python 依赖 ───────────────────────────────────────────
info "安装 Python 依赖..."
pip3 install --quiet flask cryptography 2>/dev/null || \
    pip3 install flask cryptography || warn "pip 安装部分失败"

# pyodbc（可选，用于 SQL Server）
pip3 install --quiet pyodbc 2>/dev/null && info "pyodbc 安装成功" || \
    warn "pyodbc 安装失败（SQL Server 监控将使用 pymssql 回退）"

# pymssql（备用）
pip3 install --quiet pymssql 2>/dev/null && info "pymssql 安装成功" || \
    warn "pymssql 安装失败（SQL Server 监控功能不可用）"

# ── FreeTDS ODBC 配置 ──────────────────────────────────────────
info "配置 FreeTDS ODBC..."
ODBCINI=/etc/odbcinst.ini
if ! grep -q "FreeTDS" "$ODBCINI" 2>/dev/null; then
    # 查找 libtdsodbc 位置
    TDSLIB=$(find /usr /lib64 -name "libtdsodbc.so*" 2>/dev/null | head -1)
    if [[ -n "$TDSLIB" ]]; then
        cat >> "$ODBCINI" << EOF

[FreeTDS]
Description = FreeTDS ODBC Driver
Driver = $TDSLIB
Setup = $TDSLIB
FileUsage = 1
UsageCount = 1
EOF
        info "FreeTDS ODBC 配置完成: $TDSLIB"
    else
        warn "未找到 libtdsodbc.so，SQL Server 监控可能受限"
    fi
else
    info "FreeTDS ODBC 已配置"
fi

# FreeTDS 全局版本
FREETDS_CONF=/etc/freetds.conf
if [[ -f "$FREETDS_CONF" ]]; then
    if ! grep -q "tds version = 7.4" "$FREETDS_CONF" 2>/dev/null; then
        sed -i 's/tds version = .*/tds version = 7.4/' "$FREETDS_CONF" 2>/dev/null || true
    fi
fi

# ── 复制程序文件 ───────────────────────────────────────────────
info "安装程序文件到 $INSTALL_DIR ..."
mkdir -p "$INSTALL_DIR/templates"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

for f in db_init.py monitor_server.py monitor_probe.py passwd.py; do
    [[ -f "$SCRIPT_DIR/$f" ]] && cp "$SCRIPT_DIR/$f" "$INSTALL_DIR/" || warn "未找到 $f"
done
for f in templates/monitor.html templates/login.html; do
    [[ -f "$SCRIPT_DIR/$f" ]] && cp "$SCRIPT_DIR/$f" "$INSTALL_DIR/$f" || warn "未找到 $f"
done

chmod +x "$INSTALL_DIR"/*.py

# ── 初始化数据库 ───────────────────────────────────────────────
info "初始化数据库..."
python3 "$INSTALL_DIR/db_init.py"

# ── 初始化密码文件 ─────────────────────────────────────────────
PASSWD_FILE="$INSTALL_DIR/.passwd"
if [[ ! -f "$PASSWD_FILE" ]]; then
    cat > "$PASSWD_FILE" << 'EOF'
# 监控平台登录凭据
# 修改后重启服务生效：systemctl restart monitor
username=admin
password=admin
EOF
    chmod 600 "$PASSWD_FILE"
    warn "首次安装，默认账号 admin/admin，请登录后立即修改密码！"
else
    info "密码文件已存在，跳过（保留原密码）"
fi

# ── 安装 systemd 服务 ──────────────────────────────────────────
info "安装 systemd 服务..."
cp "$SCRIPT_DIR/monitor.service" /etc/systemd/system/
cp "$SCRIPT_DIR/probe.service"   /etc/systemd/system/
systemctl daemon-reload

# ── 启动服务 ───────────────────────────────────────────────────
info "启动服务..."
systemctl enable monitor.service probe.service
systemctl restart monitor.service
sleep 2
systemctl restart probe.service

# ── 防火墙放行（可选） ──────────────────────────────────────────
if command -v firewall-cmd &>/dev/null && systemctl is-active firewalld &>/dev/null; then
    firewall-cmd --permanent --add-port=8090/tcp &>/dev/null || true
    firewall-cmd --reload &>/dev/null || true
    info "防火墙已放行 8090 端口"
fi

# ── 完成 ──────────────────────────────────────────────────────
SERVER_IP=$(hostname -I | awk '{print $1}')
echo ""
echo -e "${GREEN}============================================================${NC}"
echo -e "${GREEN}  安装完成！${NC}"
echo -e "${GREEN}============================================================${NC}"
echo -e "  访问地址: ${YELLOW}http://${SERVER_IP}:8090${NC}"
echo -e "  管理登录: ${YELLOW}http://${SERVER_IP}:8090/login${NC}"
echo -e "  默认账号: admin / admin（请立即修改）"
echo ""
echo "  常用命令:"
echo "    systemctl status monitor     # Web服务状态"
echo "    systemctl status probe       # 探针状态"
echo "    systemctl restart monitor    # 重启Web服务"
echo "    systemctl restart probe      # 重启探针"
echo "    tail -f /opt/monitor/probe.log   # 查看探针日志"
echo "    python3 /opt/monitor/passwd.py   # 命令行改密"
echo -e "${GREEN}============================================================${NC}"

# ── 验证服务 ──────────────────────────────────────────────────
sleep 2
if systemctl is-active monitor.service &>/dev/null; then
    info "monitor.service 运行正常 ✅"
else
    warn "monitor.service 启动异常，请检查: journalctl -u monitor -n 30"
fi
if systemctl is-active probe.service &>/dev/null; then
    info "probe.service 运行正常 ✅"
else
    warn "probe.service 启动异常，请检查: journalctl -u probe -n 30"
fi
