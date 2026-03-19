# 系统可用性监控平台

轻量级 IT 基础设施可用性监控系统，支持 HTTP/HTTPS、SQL Server、Oracle 多种协议探测，提供实时看板、故障告警、多渠道通知。
<img width="1920" height="945" alt="image" src="https://github.com/user-attachments/assets/54afb685-ce05-416a-add6-7de9895e4d2a" />

## 功能特性

- **多协议探测**：HTTP/HTTPS（跟随重定向、忽略SSL）、SQL Server（pyodbc+FreeTDS / pymssql 双引擎）、Oracle（cx_Oracle）
- **实时看板**：系统热力图（当前实时状态）、活动告警列表、时间轴色块、响应时间折线图
- **可用性统计**：全天可用性 + 核心时段可用性（8:00–21:00）、MTTR、MTBF
- **故障事件**：自动开启/关闭故障事件，支持手动备注
- **历史对比**：今天 / 昨天 / 本周 / 本月 / 最近7天 / 最近30天
- **多渠道告警**：邮件 SMTP、企业微信、钉钉、飞书、自定义 Webhook
- **系统管理**：拖拽排序、CSV 导入导出、测试连接
- **访问控制**：看板公开访问，管理功能需登录

## 快速部署

### 环境要求

- Python 3.8+
- OpenEuler 22.03 / CentOS 7+ / RHEL 8+ / Debian 11+
- curl（系统预装）

### 一键安装

```bash
tar -xzf monitor_deploy.tar.gz
cd monitor_deploy
bash install.sh
```

安装完成后访问 `http://<服务器IP>:8090`，默认账号 `admin / admin`，**请立即修改密码**。

### 手动安装

```bash
# 安装依赖
pip3 install flask cryptography pyodbc pymssql

# 复制文件
mkdir -p /opt/monitor/templates
cp *.py /opt/monitor/
cp templates/*.html /opt/monitor/templates/

# 初始化数据库
python3 /opt/monitor/db_init.py

# 启动
python3 /opt/monitor/monitor_server.py &
python3 /opt/monitor/monitor_probe.py &
```

## 目录结构

```
/opt/monitor/
├── monitor_server.py   # Flask Web 服务（端口 8090）
├── monitor_probe.py    # 探针常驻进程（每分钟轮询）
├── db_init.py          # 数据库初始化 + 密码加解密
├── passwd.py           # 命令行改密工具
├── monitor.db          # SQLite 数据库（WAL 模式）
├── probe.log           # 探针运行日志
├── .passwd             # 登录凭据（600权限）
├── .secret_key         # 数据库密码加密密钥（600权限）
└── templates/
    ├── monitor.html
    └── login.html
```

## 常用命令

```bash
# 服务管理
systemctl status  monitor probe
systemctl restart monitor probe
systemctl stop    monitor probe

# 日志查看
tail -f /opt/monitor/probe.log
journalctl -u monitor -f
journalctl -u probe   -f

# 修改密码（交互式）
python3 /opt/monitor/passwd.py

# 修改密码（直接）
python3 /opt/monitor/passwd.py -u admin -p NewPassword123
```

## 告警通知配置

登录后点击顶栏「🔔 通知设置」，支持以下渠道：

| 渠道 | 说明 |
|------|------|
| 📧 邮件 SMTP | 支持 SSL/TLS，多收件人逗号分隔 |
| 💬 企业微信 | 群机器人 Webhook |
| 📱 钉钉 | 群机器人 Webhook |
| 🪁 飞书 | 群机器人 Webhook |
| 🔗 自定义 Webhook | 任意 POST 接口，支持 Body 模板 |

触发规则：DOWN 故障 / DEGRADED 降级 / 故障恢复，可设置防骚扰冷却时间。

## SQL Server 连接说明

系统使用 **pyodbc + FreeTDS** 作为主要驱动，pymssql 作为回退。

TDS 版本选择：

| SQL Server 版本 | TDS 版本 |
|----------------|---------|
| 2008 R2        | 7.2     |
| 2012 / 2014    | 7.3     |
| 2016 / 2017 / 2019 / 2022 | 7.4 |

## 数据库账号最小权限

**SQL Server**：
```sql
CREATE LOGIN monitor WITH PASSWORD='YourPassword';
USE YourDatabase;
CREATE USER monitor FOR LOGIN monitor;
GRANT CONNECT TO monitor;
```

**Oracle**：
```sql
CREATE USER monitor IDENTIFIED BY YourPassword;
GRANT CREATE SESSION TO monitor;
```

## 迁移说明

迁移时需复制以下三个文件（保持相对路径不变）：
- `monitor.db`（数据）
- `.passwd`（登录密码）
- `.secret_key`（数据库密码加密密钥，丢失则所有数据库密码失效）

## 修改核心时段

编辑 `monitor_server.py` 和 `monitor_probe.py` 顶部：

```python
CORE_START = 8   # 核心时段开始（小时）
CORE_END   = 21  # 核心时段结束（小时）
```

修改后重启服务生效。

## License

MIT
