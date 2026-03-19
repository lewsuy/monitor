#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
系统可用性探针 - 常驻进程
路径: /opt/monitor/monitor_probe.py
由 systemd probe.service 管理
"""

import os
import sys
import time
import socket
import subprocess
import datetime
import sqlite3
import logging
import json
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

BASE_DIR = "/opt/monitor"
DB_PATH  = f"{BASE_DIR}/monitor.db"
LOG_FILE = f"{BASE_DIR}/probe.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger("probe")

CORE_START = 8
CORE_END   = 21

sys.path.insert(0, BASE_DIR)
try:
    from db_init import decrypt_password, init_db
except ImportError:
    def decrypt_password(s): return s
    def init_db(): pass


# ════════════════════════════════════════════
#  数据库
# ════════════════════════════════════════════

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


# ════════════════════════════════════════════
#  探测函数
# ════════════════════════════════════════════

def probe_http(url: str, expected_codes: str, timeout: int) -> dict:
    try:
        cmd = [
            "curl", "-o", "/dev/null", "-s",
            "-w", "%{http_code}|%{time_total}",
            "-L", "-k",
            "--max-time", str(timeout),
            "--connect-timeout", "5",
            url
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 3)
        out = result.stdout.strip()
        if "|" not in out:
            return {"status": "DOWN", "response_ms": 0,
                    "status_code": 0, "error_msg": f"curl异常: {result.stderr[:80]}"}
        code_str, time_str = out.split("|", 1)
        code = int(code_str) if code_str.isdigit() else 0
        ms   = int(float(time_str) * 1000)
        allowed = [c.strip() for c in expected_codes.split(",") if c.strip()]
        if code == 0:
            return {"status": "DOWN", "response_ms": ms,
                    "status_code": 0, "error_msg": "连接失败或超时"}
        elif str(code) in allowed or (200 <= code < 400):
            return {"status": "UP", "response_ms": ms,
                    "status_code": code, "error_msg": ""}
        else:
            return {"status": "DOWN", "response_ms": ms,
                    "status_code": code, "error_msg": f"HTTP {code}，期望 {expected_codes}"}
    except subprocess.TimeoutExpired:
        return {"status": "DOWN", "response_ms": timeout * 1000,
                "status_code": 0, "error_msg": "探测超时"}
    except Exception as e:
        return {"status": "DOWN", "response_ms": 0,
                "status_code": 0, "error_msg": str(e)[:100]}


def probe_tcp(host: str, port: int, timeout: int = 5) -> tuple:
    start = time.time()
    try:
        s = socket.create_connection((host, port), timeout=timeout)
        s.close()
        return True, int((time.time() - start) * 1000), ""
    except socket.timeout:
        return False, timeout * 1000, "TCP连接超时"
    except ConnectionRefusedError:
        return False, 0, "端口拒绝连接"
    except OSError as e:
        return False, 0, str(e)[:80]


def probe_mssql(host, port, db, user, password, timeout, tds_version="7.2") -> dict:
    tcp_ok, tcp_ms, tcp_err = probe_tcp(host, port)
    if not tcp_ok:
        return {"status": "DOWN", "response_ms": tcp_ms,
                "status_code": 0, "error_msg": tcp_err}
    # pyodbc + FreeTDS
    try:
        import pyodbc
        conn_str = (
            f"DRIVER={{FreeTDS}};SERVER={host};PORT={port};"
            f"DATABASE={db};UID={user};PWD={password};"
            f"TDS_Version={tds_version};LoginTimeout=5;Timeout={timeout};"
        )
        start = time.time()
        conn  = pyodbc.connect(conn_str, timeout=timeout)
        conn.execute("SELECT 1")
        conn.close()
        return {"status": "UP", "response_ms": int((time.time() - start) * 1000),
                "status_code": 0, "error_msg": ""}
    except ImportError:
        pass
    except Exception as e:
        return {"status": "DEGRADED", "response_ms": tcp_ms,
                "status_code": 0, "error_msg": f"SQL失败(pyodbc): {str(e)[:100]}"}
    # 回退 pymssql
    try:
        import pymssql
        start = time.time()
        conn  = pymssql.connect(server=host, port=str(port), user=user,
                                password=password, database=db,
                                timeout=timeout, login_timeout=5)
        conn.cursor().execute("SELECT 1")
        conn.close()
        return {"status": "UP", "response_ms": int((time.time() - start) * 1000),
                "status_code": 0, "error_msg": ""}
    except Exception as e:
        return {"status": "DEGRADED", "response_ms": tcp_ms,
                "status_code": 0, "error_msg": f"SQL失败(pymssql): {str(e)[:100]}"}


def probe_oracle(host, port, db, user, password, timeout) -> dict:
    tcp_ok, tcp_ms, tcp_err = probe_tcp(host, port)
    if not tcp_ok:
        return {"status": "DOWN", "response_ms": tcp_ms,
                "status_code": 0, "error_msg": tcp_err}
    try:
        import cx_Oracle
        dsn  = cx_Oracle.makedsn(host, port, service_name=db)
        start = time.time()
        conn = cx_Oracle.connect(user=user, password=password, dsn=dsn, timeout=timeout)
        conn.cursor().execute("SELECT 1 FROM DUAL")
        conn.close()
        return {"status": "UP", "response_ms": int((time.time() - start) * 1000),
                "status_code": 0, "error_msg": ""}
    except ImportError:
        return {"status": "UP", "response_ms": tcp_ms,
                "status_code": 0, "error_msg": "[仅TCP] cx_Oracle未安装"}
    except Exception as e:
        return {"status": "DEGRADED", "response_ms": tcp_ms,
                "status_code": 0, "error_msg": f"Oracle失败: {str(e)[:100]}"}


def probe_system(s: dict) -> dict:
    stype   = s["type"]
    timeout = s["timeout_sec"] or 10
    if stype == "http":
        return probe_http(s["url"], s["expected_code"] or "200,301,302", timeout)
    elif stype == "mssql":
        return probe_mssql(s["db_host"], s["db_port"], s["db_name"],
                           s["db_user"], decrypt_password(s["db_password"]),
                           timeout, s.get("tds_version") or "7.2")
    elif stype == "oracle":
        return probe_oracle(s["db_host"], s["db_port"], s["db_name"],
                            s["db_user"], decrypt_password(s["db_password"]), timeout)
    return {"status": "UNKNOWN", "response_ms": 0, "status_code": 0,
            "error_msg": f"未知类型: {stype}"}


# ════════════════════════════════════════════
#  故障事件
# ════════════════════════════════════════════

def update_incidents(conn, system_id, status, check_time, error_msg):
    open_inc = conn.execute("""
        SELECT id, start_time FROM availability_incidents
        WHERE system_id=? AND end_time IS NULL
        ORDER BY start_time DESC LIMIT 1
    """, (system_id,)).fetchone()

    if status in ("DOWN", "DEGRADED"):
        if not open_inc:
            conn.execute("""
                INSERT INTO availability_incidents (system_id, start_time, error_msg)
                VALUES (?,?,?)
            """, (system_id, check_time, error_msg))
            log.warning(f"[故障开始] sid={system_id} {check_time} {error_msg[:60]}")
    else:
        if open_inc:
            start_dt = datetime.datetime.fromisoformat(open_inc["start_time"])
            end_dt   = datetime.datetime.fromisoformat(check_time)
            duration = max(0, int((end_dt - start_dt).total_seconds()))
            conn.execute("""
                UPDATE availability_incidents
                SET end_time=?, duration_seconds=? WHERE id=?
            """, (check_time, duration, open_inc["id"]))
            log.info(f"[故障恢复] sid={system_id} 持续{duration}s")


# ════════════════════════════════════════════
#  告警通知
# ════════════════════════════════════════════

def load_notify_config(conn) -> dict:
    """加载通知配置"""
    rows = conn.execute(
        "SELECT channel, enabled, config_json FROM notify_config"
    ).fetchall()
    cfg = {}
    for r in rows:
        try:
            cfg[r["channel"]] = {
                "enabled": bool(r["enabled"]),
                "config": json.loads(r["config_json"] or "{}")
            }
        except Exception:
            pass
    return cfg


def load_notify_rules(conn) -> dict:
    row = conn.execute("SELECT * FROM notify_rules WHERE id=1").fetchone()
    if row:
        return dict(row)
    return {"notify_on_down": 1, "notify_on_degraded": 1,
            "notify_on_recover": 1, "cooldown_minutes": 30}


def check_cooldown(conn, system_id, channel, cooldown_minutes) -> bool:
    """True = 可以发送（不在冷却期）"""
    cutoff = (datetime.datetime.now() -
              datetime.timedelta(minutes=cooldown_minutes)).strftime("%Y-%m-%d %H:%M:%S")
    row = conn.execute("""
        SELECT id FROM notify_log
        WHERE system_id=? AND channel=? AND sent_at>=? AND success=1
        ORDER BY sent_at DESC LIMIT 1
    """, (system_id, channel, cutoff)).fetchone()
    return row is None


def log_notify(conn, system_id, channel, event_type, success, error_msg=""):
    conn.execute("""
        INSERT INTO notify_log (system_id, channel, event_type, sent_at, success, error_msg)
        VALUES (?,?,?,datetime('now','localtime'),?,?)
    """, (system_id, channel, event_type, 1 if success else 0, error_msg))


def build_message(system_name, status, error_msg, event_type) -> dict:
    """构建通知消息内容"""
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    icons   = {"DOWN": "🔴", "DEGRADED": "⚠️", "UP": "✅"}
    icon    = icons.get(status, "❓")
    labels  = {"DOWN": "故障告警", "DEGRADED": "降级告警", "UP": "故障恢复"}
    label   = labels.get(event_type, "状态变化")

    title = f"{icon} 【{label}】{system_name}"
    body  = (
        f"系统名称：{system_name}\n"
        f"当前状态：{status}\n"
        f"发生时间：{now_str}\n"
        f"原因说明：{error_msg or '无'}"
    )
    html_body = (
        f"<h3>{icon} {label}</h3>"
        f"<table style='border-collapse:collapse'>"
        f"<tr><td style='padding:4px 12px 4px 0;color:#666'>系统名称</td>"
        f"<td><b>{system_name}</b></td></tr>"
        f"<tr><td style='padding:4px 12px 4px 0;color:#666'>当前状态</td>"
        f"<td><b style='color:{'#e53935' if status=='DOWN' else '#fb8c00' if status=='DEGRADED' else '#43a047'}'>"
        f"{status}</b></td></tr>"
        f"<tr><td style='padding:4px 12px 4px 0;color:#666'>发生时间</td>"
        f"<td>{now_str}</td></tr>"
        f"<tr><td style='padding:4px 12px 4px 0;color:#666'>原因说明</td>"
        f"<td>{error_msg or '无'}</td></tr>"
        f"</table>"
    )
    return {"title": title, "body": body, "html_body": html_body}


def send_email(cfg: dict, msg: dict) -> tuple:
    """发送邮件，返回 (success, error)"""
    try:
        smtp_host = cfg.get("smtp_host", "")
        smtp_port = int(cfg.get("smtp_port", 465))
        username  = cfg.get("username", "")
        password  = cfg.get("password", "")
        from_addr = cfg.get("from_addr", username)
        to_addrs  = [x.strip() for x in cfg.get("to_addrs", "").split(",") if x.strip()]
        ssl_mode  = cfg.get("ssl_mode", "ssl")

        if not smtp_host or not to_addrs:
            return False, "SMTP配置不完整"

        mail = MIMEMultipart("alternative")
        mail["Subject"] = msg["title"]
        mail["From"]    = from_addr
        mail["To"]      = ", ".join(to_addrs)
        mail.attach(MIMEText(msg["body"], "plain", "utf-8"))
        mail.attach(MIMEText(msg["html_body"], "html", "utf-8"))

        if ssl_mode == "ssl":
            import ssl
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(smtp_host, smtp_port, context=ctx, timeout=10) as s:
                if username:
                    s.login(username, password)
                s.sendmail(from_addr, to_addrs, mail.as_string())
        elif ssl_mode == "tls":
            with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as s:
                s.starttls()
                if username:
                    s.login(username, password)
                s.sendmail(from_addr, to_addrs, mail.as_string())
        else:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as s:
                if username:
                    s.login(username, password)
                s.sendmail(from_addr, to_addrs, mail.as_string())
        return True, ""
    except Exception as e:
        return False, str(e)[:200]


def send_wecom(cfg: dict, msg: dict) -> tuple:
    """企业微信群机器人"""
    try:
        import urllib.request
        webhook = cfg.get("webhook_url", "")
        if not webhook:
            return False, "Webhook URL为空"
        payload = json.dumps({
            "msgtype": "text",
            "text": {"content": msg["title"] + "\n" + msg["body"]}
        }, ensure_ascii=False).encode()
        req = urllib.request.Request(
            webhook, data=payload,
            headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            if result.get("errcode", -1) == 0:
                return True, ""
            return False, result.get("errmsg", "未知错误")
    except Exception as e:
        return False, str(e)[:200]


def send_dingtalk(cfg: dict, msg: dict) -> tuple:
    """钉钉群机器人"""
    try:
        import urllib.request
        webhook = cfg.get("webhook_url", "")
        if not webhook:
            return False, "Webhook URL为空"
        payload = json.dumps({
            "msgtype": "text",
            "text": {"content": msg["title"] + "\n" + msg["body"]}
        }, ensure_ascii=False).encode()
        req = urllib.request.Request(
            webhook, data=payload,
            headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            if result.get("errcode", -1) == 0:
                return True, ""
            return False, result.get("errmsg", "未知错误")
    except Exception as e:
        return False, str(e)[:200]


def send_feishu(cfg: dict, msg: dict) -> tuple:
    """飞书群机器人"""
    try:
        import urllib.request
        webhook = cfg.get("webhook_url", "")
        if not webhook:
            return False, "Webhook URL为空"
        payload = json.dumps({
            "msg_type": "text",
            "content": {"text": msg["title"] + "\n" + msg["body"]}
        }, ensure_ascii=False).encode()
        req = urllib.request.Request(
            webhook, data=payload,
            headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            if result.get("code", -1) == 0:
                return True, ""
            return False, result.get("msg", "未知错误")
    except Exception as e:
        return False, str(e)[:200]


def send_webhook(cfg: dict, msg: dict) -> tuple:
    """自定义 Webhook"""
    try:
        import urllib.request
        webhook = cfg.get("webhook_url", "")
        if not webhook:
            return False, "Webhook URL为空"
        custom_body = cfg.get("body_template", "")
        if custom_body:
            body_str = custom_body.replace("{title}", msg["title"])\
                                  .replace("{body}", msg["body"])
        else:
            body_str = json.dumps({
                "title": msg["title"], "content": msg["body"]
            }, ensure_ascii=False)
        method  = cfg.get("method", "POST").upper()
        headers = {"Content-Type": cfg.get("content_type", "application/json")}
        req = urllib.request.Request(
            webhook, data=body_str.encode(),
            headers=headers, method=method
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status < 400, ""
    except Exception as e:
        return False, str(e)[:200]


CHANNEL_SENDERS = {
    "email":   send_email,
    "wecom":   send_wecom,
    "dingtalk":send_dingtalk,
    "feishu":  send_feishu,
    "webhook": send_webhook,
}


def dispatch_notify(conn, system_id, system_name, status,
                    error_msg, event_type):
    """按配置分发通知"""
    rules  = load_notify_rules(conn)
    notify = False
    if event_type == "DOWN"     and rules.get("notify_on_down"):     notify = True
    if event_type == "DEGRADED" and rules.get("notify_on_degraded"): notify = True
    if event_type == "RECOVER"  and rules.get("notify_on_recover"):  notify = True
    if not notify:
        return

    cfg_map  = load_notify_config(conn)
    cooldown = rules.get("cooldown_minutes", 30)
    msg      = build_message(system_name, status, error_msg, event_type)

    for channel, sender in CHANNEL_SENDERS.items():
        ch_cfg = cfg_map.get(channel, {})
        if not ch_cfg.get("enabled"):
            continue
        # 恢复通知不受冷却限制
        if event_type != "RECOVER" and not check_cooldown(conn, system_id, channel, cooldown):
            log.info(f"[通知] {channel} 冷却中，跳过 sid={system_id}")
            continue
        ok, err = sender(ch_cfg["config"], msg)
        log_notify(conn, system_id, channel, event_type, ok, err)
        if ok:
            log.info(f"[通知] {channel} 发送成功 sid={system_id} {event_type}")
        else:
            log.warning(f"[通知] {channel} 发送失败 sid={system_id}: {err}")


# ════════════════════════════════════════════
#  主探测逻辑
# ════════════════════════════════════════════

def is_core_time() -> bool:
    return CORE_START <= datetime.datetime.now().hour < CORE_END


def load_probe_state(conn) -> dict:
    rows = conn.execute(
        "SELECT system_id, last_probe_time, last_status FROM probe_state"
    ).fetchall()
    return {r["system_id"]: {
        "time": r["last_probe_time"], "status": r["last_status"]
    } for r in rows}


def should_probe(system_id, interval_minutes, probe_state) -> bool:
    st = probe_state.get(system_id, {})
    last = st.get("time", "")
    if not last:
        return True
    try:
        elapsed = (datetime.datetime.now() -
                   datetime.datetime.fromisoformat(last)).total_seconds()
        return elapsed >= interval_minutes * 60
    except Exception:
        return True


def run_once():
    try:
        conn = get_db()
    except Exception as e:
        log.error(f"数据库连接失败: {e}")
        return

    systems = conn.execute("""
        SELECT id, name, type, poll_interval, timeout_sec,
               url, expected_code, url_secondary,
               db_host, db_port, db_name, db_user, db_password,
               tds_version
        FROM systems WHERE enabled=1
        ORDER BY sort_order ASC, id ASC
    """).fetchall()

    if not systems:
        conn.close()
        return

    probe_state = load_probe_state(conn)
    now_str     = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    is_core     = 1 if is_core_time() else 0
    probed      = 0

    for s in systems:
        sid      = s["id"]
        interval = s["poll_interval"] or 5
        if not should_probe(sid, interval, probe_state):
            continue

        result = probe_system(dict(s))
        prev   = probe_state.get(sid, {}).get("status", "")

        icon = {"UP": "✅", "DOWN": "❌", "DEGRADED": "⚠️"}.get(result["status"], "❓")
        log.info(f"{icon} [{sid:2d}] {s['name']:<14} {result['status']:<9} "
                 f"{result['response_ms']:>5}ms  {result['error_msg'][:50]}")

        # 写探测记录
        conn.execute("""
            INSERT INTO availability_records
            (system_id, check_time, status, response_ms, status_code, error_msg, is_core)
            VALUES (?,?,?,?,?,?,?)
        """, (sid, now_str, result["status"], result["response_ms"],
              result["status_code"], result["error_msg"], is_core))

        # 双端点副端点
        if s.get("url_secondary"):
            sec = probe_http(s["url_secondary"],
                             s["expected_code"] or "200,301,302",
                             s["timeout_sec"] or 10)
            conn.execute("""
                INSERT INTO availability_records
                (system_id, check_time, status, response_ms, status_code, error_msg, is_core)
                VALUES (?,?,?,?,?,?,?)
            """, (-sid, now_str, sec["status"], sec["response_ms"],
                  sec["status_code"], sec["error_msg"], is_core))

        # 故障事件
        update_incidents(conn, sid, result["status"], now_str, result["error_msg"])

        # 状态变化 → 发通知
        cur = result["status"]
        if prev and cur != prev:
            if cur in ("DOWN", "DEGRADED"):
                event = cur
            elif cur == "UP" and prev in ("DOWN", "DEGRADED"):
                event = "RECOVER"
            else:
                event = None
            if event:
                try:
                    dispatch_notify(conn, sid, s["name"], cur,
                                    result["error_msg"], event)
                except Exception as e:
                    log.error(f"通知异常 sid={sid}: {e}")

        # 更新探针状态
        conn.execute("""
            INSERT OR REPLACE INTO probe_state (system_id, last_probe_time, last_status)
            VALUES (?,?,?)
        """, (sid, now_str, cur))
        probed += 1

    if probed > 0:
        conn.commit()
        log.info(f"本轮完成 {probed}/{len(systems)} 个系统")
    conn.close()


def main():
    log.info("=" * 50)
    log.info("监控探针启动")
    log.info(f"数据库: {DB_PATH}")
    log.info("=" * 50)
    try:
        init_db()
    except Exception as e:
        log.error(f"数据库初始化失败: {e}")
        sys.exit(1)
    while True:
        try:
            run_once()
        except Exception as e:
            log.error(f"探测异常: {e}", exc_info=True)
        time.sleep(60)


if __name__ == "__main__":
    main()
