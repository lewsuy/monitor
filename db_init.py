#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据库初始化 + 密码加解密工具
路径: /opt/monitor/db_init.py
"""

import sqlite3
import os
import base64

DB_PATH  = "/opt/monitor/monitor.db"
KEY_PATH = "/opt/monitor/.secret_key"


# ════════════════════════════════════════════
#  密码加解密
# ════════════════════════════════════════════

def get_or_create_key() -> bytes:
    if os.path.exists(KEY_PATH):
        with open(KEY_PATH, "rb") as f:
            return f.read().strip()
    from cryptography.fernet import Fernet
    key = Fernet.generate_key()
    with open(KEY_PATH, "wb") as f:
        f.write(key)
    os.chmod(KEY_PATH, 0o600)
    return key


def encrypt_password(plain: str) -> str:
    if not plain:
        return ""
    try:
        from cryptography.fernet import Fernet
        return Fernet(get_or_create_key()).encrypt(plain.encode()).decode()
    except ImportError:
        return "b64:" + base64.b64encode(plain.encode()).decode()


def decrypt_password(cipher: str) -> str:
    if not cipher:
        return ""
    try:
        if cipher.startswith("b64:"):
            return base64.b64decode(cipher[4:]).decode()
        from cryptography.fernet import Fernet
        return Fernet(get_or_create_key()).decrypt(cipher.encode()).decode()
    except Exception:
        return cipher


# ════════════════════════════════════════════
#  数据库连接
# ════════════════════════════════════════════

def get_db(db_path=DB_PATH):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ════════════════════════════════════════════
#  数据库初始化
# ════════════════════════════════════════════

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with get_db() as conn:

        # ── 监控系统配置表 ──────────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS systems (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                name          TEXT    NOT NULL,
                type          TEXT    NOT NULL CHECK(type IN ('http','mssql','oracle')),
                enabled       INTEGER NOT NULL DEFAULT 1,
                poll_interval INTEGER NOT NULL DEFAULT 5,
                timeout_sec   INTEGER NOT NULL DEFAULT 10,
                url           TEXT    DEFAULT '',
                expected_code TEXT    DEFAULT '200,301,302',
                url_secondary TEXT    DEFAULT '',
                db_host       TEXT    DEFAULT '',
                db_port       INTEGER DEFAULT 1433,
                db_name       TEXT    DEFAULT '',
                db_user       TEXT    DEFAULT '',
                db_password   TEXT    DEFAULT '',
                tds_version   TEXT    DEFAULT '7.2',
                sort_order    INTEGER DEFAULT 9999,
                created_at    TEXT    DEFAULT (datetime('now','localtime')),
                updated_at    TEXT    DEFAULT (datetime('now','localtime'))
            )
        """)

        # ── 探测记录表 ──────────────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS availability_records (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                system_id   INTEGER NOT NULL,
                check_time  TEXT    NOT NULL,
                status      TEXT    NOT NULL CHECK(status IN ('UP','DOWN','DEGRADED','UNKNOWN')),
                response_ms INTEGER DEFAULT 0,
                status_code INTEGER DEFAULT 0,
                error_msg   TEXT    DEFAULT '',
                is_core     INTEGER DEFAULT 0,
                FOREIGN KEY(system_id) REFERENCES systems(id) ON DELETE CASCADE
            )
        """)

        # ── 故障事件表 ──────────────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS availability_incidents (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                system_id        INTEGER NOT NULL,
                start_time       TEXT    NOT NULL,
                end_time         TEXT,
                duration_seconds INTEGER DEFAULT 0,
                error_msg        TEXT    DEFAULT '',
                note             TEXT    DEFAULT '',
                FOREIGN KEY(system_id) REFERENCES systems(id) ON DELETE CASCADE
            )
        """)

        # ── 探针状态表 ──────────────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS probe_state (
                system_id       INTEGER PRIMARY KEY,
                last_probe_time TEXT DEFAULT '',
                last_status     TEXT DEFAULT '',
                FOREIGN KEY(system_id) REFERENCES systems(id) ON DELETE CASCADE
            )
        """)

        # ── 通知配置表 ──────────────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS notify_config (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                channel     TEXT NOT NULL UNIQUE,
                enabled     INTEGER DEFAULT 0,
                config_json TEXT DEFAULT '{}',
                updated_at  TEXT DEFAULT (datetime('now','localtime'))
            )
        """)

        # ── 通知记录表（防骚扰） ────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS notify_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                system_id   INTEGER NOT NULL,
                channel     TEXT NOT NULL,
                event_type  TEXT NOT NULL,
                sent_at     TEXT NOT NULL,
                success     INTEGER DEFAULT 1,
                error_msg   TEXT DEFAULT ''
            )
        """)

        # ── 通知规则表 ──────────────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS notify_rules (
                id                INTEGER PRIMARY KEY,
                notify_on_down    INTEGER DEFAULT 1,
                notify_on_degraded INTEGER DEFAULT 1,
                notify_on_recover INTEGER DEFAULT 1,
                cooldown_minutes  INTEGER DEFAULT 30,
                updated_at        TEXT DEFAULT (datetime('now','localtime'))
            )
        """)

        # 默认通知规则（只插入一次）
        conn.execute("""
            INSERT OR IGNORE INTO notify_rules
            (id, notify_on_down, notify_on_degraded, notify_on_recover, cooldown_minutes)
            VALUES (1, 1, 1, 1, 30)
        """)

        # 默认通知渠道（插入占位，方便前端读取）
        for channel in ('email', 'wecom', 'dingtalk', 'feishu', 'webhook'):
            conn.execute("""
                INSERT OR IGNORE INTO notify_config (channel, enabled, config_json)
                VALUES (?, 0, '{}')
            """, (channel,))

        # ── 索引 ─────────────────────────────────────────────────────
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_records_system_time
            ON availability_records(system_id, check_time)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_incidents_system
            ON availability_incidents(system_id, start_time)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_notify_log_system
            ON notify_log(system_id, sent_at)
        """)

        conn.commit()

    print(f"[DB] 初始化完成: {DB_PATH}")


if __name__ == "__main__":
    init_db()
    print("[DB] 密钥:", KEY_PATH)
    print("[DB] 加解密测试:", decrypt_password(encrypt_password("test_123")))
