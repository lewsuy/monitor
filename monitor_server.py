#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
系统可用性监控平台 - Web 服务
路径: /opt/monitor/monitor_server.py
"""

# ════════════════════════════════════════════
#  配置区
# ════════════════════════════════════════════

PORT        = 8090
DB_PATH     = "/opt/monitor/monitor.db"
PASSWD_FILE = "/opt/monitor/.passwd"
SECRET_KEY  = "monitor_flask_secret_2026_change_me"
CORE_START  = 8
CORE_END    = 21

# ════════════════════════════════════════════

import sqlite3, json, os, sys, datetime, time as _time
from functools import wraps
from flask import Flask, render_template, request, jsonify, session, redirect, url_for

BASE_DIR = "/opt/monitor"
sys.path.insert(0, BASE_DIR)

try:
    from db_init import init_db, encrypt_password, decrypt_password
except ImportError:
    def init_db(): pass
    def encrypt_password(s): return s
    def decrypt_password(s): return s

app = Flask(__name__)
app.secret_key = SECRET_KEY
app.permanent_session_lifetime = datetime.timedelta(days=7)


# ════════════════════════════════════════════
#  密码文件
# ════════════════════════════════════════════

def load_credentials():
    try:
        if os.path.exists(PASSWD_FILE):
            with open(PASSWD_FILE) as f:
                lines = [l.strip() for l in f if l.strip() and not l.startswith("#")]
            creds = {}
            for line in lines:
                if "=" in line:
                    k, v = line.split("=", 1)
                    creds[k.strip()] = v.strip()
            return creds.get("username", "admin"), creds.get("password", "admin")
    except Exception:
        pass
    return "admin", "admin"


def save_credentials(username, password):
    content = (
        "# 监控平台登录凭据\n"
        "# 修改后重启服务生效：systemctl restart monitor\n"
        f"username={username}\n"
        f"password={password}\n"
    )
    with open(PASSWD_FILE, "w") as f:
        f.write(content)
    os.chmod(PASSWD_FILE, 0o600)


# ════════════════════════════════════════════
#  数据库
# ════════════════════════════════════════════

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ════════════════════════════════════════════
#  认证
# ════════════════════════════════════════════

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "未登录", "need_login": True}), 401
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username, password = load_credentials()
        if (request.form.get("username") == username and
                request.form.get("password") == password):
            session["logged_in"] = True
            session.permanent = True
            return redirect(url_for("index"))
        error = "用户名或密码错误"
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
def index():
    return render_template("monitor.html",
                           is_logged_in=session.get("logged_in", False))


# ════════════════════════════════════════════
#  工具函数
# ════════════════════════════════════════════

def fmt_duration(s):
    s = int(s or 0)
    if s < 60:    return f"{s}秒"
    if s < 3600:  return f"{s//60}分{s%60}秒"
    if s < 86400: return f"{s//3600}小时{(s%3600)//60}分"
    return f"{s//86400}天{(s%86400)//3600}小时"


def calc_availability(rows):
    total = len(rows)
    if not total:
        return {"availability": None, "up": 0, "down": 0, "degraded": 0, "total": 0}
    up       = sum(1 for r in rows if r["status"] == "UP")
    down     = sum(1 for r in rows if r["status"] == "DOWN")
    degraded = sum(1 for r in rows if r["status"] == "DEGRADED")
    return {"availability": round(up/total*100, 4),
            "up": up, "down": down, "degraded": degraded, "total": total}


def get_granularity(start, end):
    diff_h = (end - start).total_seconds() / 3600
    if diff_h <= 2:    return 5
    if diff_h <= 24:   return 30
    if diff_h <= 168:  return 240
    if diff_h <= 720:  return 1440
    if diff_h <= 2160: return 4320
    return 10080


def parse_dt(s):
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f"无法解析时间: {s}")


# ════════════════════════════════════════════
#  API - 认证
# ════════════════════════════════════════════

@app.route("/api/auth/status")
def api_auth_status():
    return jsonify({"logged_in": session.get("logged_in", False)})


@app.route("/api/auth/change_password", methods=["POST"])
@login_required
def api_change_password():
    data    = request.get_json()
    old_pwd = data.get("old_password", "")
    new_pwd = data.get("new_password", "")
    if not old_pwd or not new_pwd:
        return jsonify({"error": "请填写原密码和新密码"}), 400
    if len(new_pwd) < 4:
        return jsonify({"error": "新密码至少4位"}), 400
    username, password = load_credentials()
    if old_pwd != password:
        return jsonify({"error": "原密码错误"}), 403
    save_credentials(username, new_pwd)
    return jsonify({"ok": True})


# ════════════════════════════════════════════
#  API - 系统管理
# ════════════════════════════════════════════

@app.route("/api/systems", methods=["GET"])
def api_get_systems():
    with get_db() as conn:
        systems = conn.execute("""
            SELECT id, name, type, enabled, poll_interval, timeout_sec,
                   url, expected_code, url_secondary,
                   db_host, db_port, db_name, db_user,
                   tds_version, sort_order, created_at, updated_at
            FROM systems ORDER BY sort_order ASC, id ASC
        """).fetchall()
        result = []
        for s in systems:
            latest = conn.execute("""
                SELECT status, response_ms, check_time, error_msg
                FROM availability_records WHERE system_id=?
                ORDER BY check_time DESC LIMIT 1
            """, (s["id"],)).fetchone()
            open_inc = conn.execute("""
                SELECT start_time FROM availability_incidents
                WHERE system_id=? AND end_time IS NULL LIMIT 1
            """, (s["id"],)).fetchone()
            row = dict(s)
            row["current_status"]  = latest["status"]     if latest else "UNKNOWN"
            row["current_ms"]      = latest["response_ms"] if latest else 0
            row["last_check"]      = latest["check_time"]  if latest else ""
            row["last_error"]      = latest["error_msg"]   if latest else ""
            row["incident_since"]  = open_inc["start_time"] if open_inc else None
            result.append(row)
    return jsonify(result)


@app.route("/api/systems", methods=["POST"])
@login_required
def api_add_system():
    d = request.get_json()
    if not d or not d.get("name") or not d.get("type"):
        return jsonify({"error": "name 和 type 为必填项"}), 400
    stype = d.get("type")
    if stype not in ("http", "mssql", "oracle"):
        return jsonify({"error": "type 只能是 http/mssql/oracle"}), 400
    pwd_cipher = encrypt_password(d.get("db_password", "")) if d.get("db_password") else ""
    with get_db() as conn:
        max_order = conn.execute(
            "SELECT COALESCE(MAX(sort_order),0) FROM systems"
        ).fetchone()[0]
        new_order = int(d.get("sort_order", max_order + 1))
        cur = conn.execute("""
            INSERT INTO systems
            (name,type,enabled,poll_interval,timeout_sec,
             url,expected_code,url_secondary,
             db_host,db_port,db_name,db_user,db_password,
             tds_version,sort_order)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (d.get("name"), stype,
              1 if d.get("enabled", True) else 0,
              int(d.get("poll_interval", 5)),
              int(d.get("timeout_sec", 10)),
              d.get("url", ""), d.get("expected_code", "200,301,302"),
              d.get("url_secondary", ""), d.get("db_host", ""),
              int(d.get("db_port", 1433)), d.get("db_name", ""),
              d.get("db_user", ""), pwd_cipher,
              d.get("tds_version", "7.2"), new_order))
        conn.commit()
        return jsonify({"ok": True, "id": cur.lastrowid})


@app.route("/api/systems/<int:sid>", methods=["GET"])
@login_required
def api_get_system(sid):
    with get_db() as conn:
        s = conn.execute("""
            SELECT id,name,type,enabled,poll_interval,timeout_sec,
                   url,expected_code,url_secondary,
                   db_host,db_port,db_name,db_user,tds_version,sort_order
            FROM systems WHERE id=?
        """, (sid,)).fetchone()
    if not s:
        return jsonify({"error": "不存在"}), 404
    return jsonify(dict(s))


@app.route("/api/systems/<int:sid>", methods=["PUT"])
@login_required
def api_update_system(sid):
    d = request.get_json()
    if not d:
        return jsonify({"error": "无数据"}), 400
    with get_db() as conn:
        s = conn.execute("SELECT db_password FROM systems WHERE id=?", (sid,)).fetchone()
        if not s:
            return jsonify({"error": "不存在"}), 404
        pwd_plain = d.get("db_password", "")
        pwd_cipher = (encrypt_password(pwd_plain)
                      if pwd_plain and pwd_plain != "••••••••"
                      else s["db_password"])
        conn.execute("""
            UPDATE systems SET
                name=?,type=?,enabled=?,poll_interval=?,timeout_sec=?,
                url=?,expected_code=?,url_secondary=?,
                db_host=?,db_port=?,db_name=?,db_user=?,db_password=?,
                tds_version=?,sort_order=?,
                updated_at=datetime('now','localtime')
            WHERE id=?
        """, (d.get("name"), d.get("type"),
              1 if d.get("enabled", True) else 0,
              int(d.get("poll_interval", 5)), int(d.get("timeout_sec", 10)),
              d.get("url", ""), d.get("expected_code", "200,301,302"),
              d.get("url_secondary", ""), d.get("db_host", ""),
              int(d.get("db_port", 1433)), d.get("db_name", ""),
              d.get("db_user", ""), pwd_cipher,
              d.get("tds_version", "7.2"), int(d.get("sort_order", 9999)),
              sid))
        conn.commit()
    return jsonify({"ok": True})


@app.route("/api/systems/<int:sid>", methods=["DELETE"])
@login_required
def api_delete_system(sid):
    with get_db() as conn:
        conn.execute("DELETE FROM systems WHERE id=?", (sid,))
        conn.commit()
    return jsonify({"ok": True})


@app.route("/api/systems/<int:sid>/toggle", methods=["POST"])
@login_required
def api_toggle_system(sid):
    with get_db() as conn:
        s = conn.execute("SELECT enabled FROM systems WHERE id=?", (sid,)).fetchone()
        if not s:
            return jsonify({"error": "不存在"}), 404
        new_val = 0 if s["enabled"] else 1
        conn.execute("UPDATE systems SET enabled=? WHERE id=?", (new_val, sid))
        conn.commit()
    return jsonify({"ok": True, "enabled": bool(new_val)})


@app.route("/api/systems/reorder", methods=["POST"])
@login_required
def api_reorder_systems():
    data = request.get_json()
    if not isinstance(data, list):
        return jsonify({"error": "需要数组"}), 400
    with get_db() as conn:
        for item in data:
            conn.execute("UPDATE systems SET sort_order=? WHERE id=?",
                         (item.get("sort_order", 9999), item.get("id")))
        conn.commit()
    return jsonify({"ok": True})


@app.route("/api/systems/export_json")
def api_export_systems():
    with get_db() as conn:
        rows = conn.execute("""
            SELECT id,name,type,enabled,poll_interval,timeout_sec,
                   url,expected_code,url_secondary,
                   db_host,db_port,db_name,db_user,tds_version,sort_order
            FROM systems ORDER BY sort_order ASC, id ASC
        """).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/systems/import", methods=["POST"])
@login_required
def api_import_systems():
    data = request.get_json()
    if not isinstance(data, list):
        return jsonify({"error": "需要JSON数组"}), 400
    success = 0
    errors  = []
    with get_db() as conn:
        max_order = conn.execute(
            "SELECT COALESCE(MAX(sort_order),0) FROM systems"
        ).fetchone()[0]
        for i, item in enumerate(data):
            try:
                if not item.get("name") or item.get("type") not in ("http","mssql","oracle"):
                    errors.append(f"{item.get('name','?')}: 名称或类型无效")
                    continue
                max_order += 1
                conn.execute("""
                    INSERT INTO systems
                    (name,type,enabled,poll_interval,timeout_sec,
                     url,expected_code,url_secondary,
                     db_host,db_port,db_name,db_user,db_password,
                     tds_version,sort_order)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (item["name"], item["type"],
                      1 if item.get("enabled", True) else 0,
                      int(item.get("poll_interval", 5)),
                      int(item.get("timeout_sec", 10)),
                      item.get("url",""), item.get("expected_code","200,301,302"),
                      item.get("url_secondary",""), item.get("db_host",""),
                      int(item.get("db_port", 1433)), item.get("db_name",""),
                      item.get("db_user",""), encrypt_password(item.get("db_password","")),
                      item.get("tds_version","7.2"),
                      int(item.get("sort_order", max_order))))
                success += 1
            except Exception as e:
                errors.append(f"{item.get('name','?')}: {str(e)}")
        conn.commit()
    return jsonify({"ok": True, "success": success, "errors": errors})


# ════════════════════════════════════════════
#  API - 测试连接
# ════════════════════════════════════════════

@app.route("/api/systems/test", methods=["POST"])
@login_required
def api_test_connection():
    d = request.get_json()
    if not d:
        return jsonify({"ok": False, "error": "无数据"}), 400
    stype   = d.get("type")
    timeout = int(d.get("timeout_sec", 10))
    try:
        if stype == "http":
            import subprocess
            url = d.get("url", "")
            if not url:
                return jsonify({"ok": False, "error": "URL为空"})
            cmd = ["curl","-o","/dev/null","-s","-w","%{http_code}|%{time_total}",
                   "-L","-k","--max-time",str(timeout),"--connect-timeout","5",url]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout+3)
            out = result.stdout.strip()
            if "|" not in out:
                return jsonify({"ok": False, "error": f"curl失败: {result.stderr[:80]}"})
            code_str, time_str = out.split("|", 1)
            code = int(code_str) if code_str.isdigit() else 0
            ms   = int(float(time_str) * 1000)
            if code == 0:
                return jsonify({"ok": False, "error": "连接失败或超时"})
            allowed = [x.strip() for x in (d.get("expected_code","200,301,302")).split(",")]
            if str(code) in allowed or (200 <= code < 400):
                return jsonify({"ok": True, "msg": f"连接成功 HTTP {code}，响应 {ms}ms"})
            return jsonify({"ok": False, "error": f"HTTP {code}，响应 {ms}ms"})

        elif stype in ("mssql", "oracle"):
            host = d.get("db_host","")
            port = int(d.get("db_port", 1433))
            db   = d.get("db_name","")
            user = d.get("db_user","")
            pwd  = d.get("db_password","")
            tds  = d.get("tds_version","7.2")
            sid  = d.get("id")
            if pwd in ("","••••••••") and sid:
                with get_db() as conn:
                    row = conn.execute("SELECT db_password FROM systems WHERE id=?",
                                       (sid,)).fetchone()
                    if row:
                        pwd = decrypt_password(row["db_password"])
            if not all([host, db, user, pwd]):
                return jsonify({"ok": False, "error": "请填写完整的数据库连接信息"})

            import socket
            try:
                s = socket.create_connection((host, port), timeout=5)
                s.close()
            except Exception as e:
                return jsonify({"ok": False, "error": f"TCP连接失败: {str(e)}"})

            if stype == "mssql":
                try:
                    import pyodbc
                    conn_str = (f"DRIVER={{FreeTDS}};SERVER={host};PORT={port};"
                                f"DATABASE={db};UID={user};PWD={pwd};"
                                f"TDS_Version={tds};LoginTimeout=5;Timeout={timeout};")
                    start = _time.time()
                    conn  = pyodbc.connect(conn_str, timeout=timeout)
                    conn.execute("SELECT 1"); conn.close()
                    ms = int((_time.time()-start)*1000)
                    return jsonify({"ok": True, "msg": f"连接成功，响应 {ms}ms (TDS{tds})"})
                except ImportError:
                    pass
                except Exception as e:
                    return jsonify({"ok": False, "error": f"SQL失败: {str(e)[:150]}"})
                try:
                    import pymssql
                    start = _time.time()
                    conn  = pymssql.connect(server=host, port=str(port), user=user,
                                            password=pwd, database=db,
                                            timeout=timeout, login_timeout=5)
                    conn.cursor().execute("SELECT 1"); conn.close()
                    ms = int((_time.time()-start)*1000)
                    return jsonify({"ok": True, "msg": f"连接成功，响应 {ms}ms (pymssql)"})
                except Exception as e:
                    return jsonify({"ok": False, "error": f"SQL失败: {str(e)[:150]}"})
            else:
                try:
                    import cx_Oracle
                    dsn  = cx_Oracle.makedsn(host, port, service_name=db)
                    start = _time.time()
                    conn = cx_Oracle.connect(user=user, password=pwd, dsn=dsn, timeout=timeout)
                    conn.cursor().execute("SELECT 1 FROM DUAL"); conn.close()
                    ms = int((_time.time()-start)*1000)
                    return jsonify({"ok": True, "msg": f"连接成功，响应 {ms}ms"})
                except Exception as e:
                    return jsonify({"ok": False, "error": f"Oracle失败: {str(e)[:150]}"})
        return jsonify({"ok": False, "error": f"未知类型: {stype}"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:150]})


# ════════════════════════════════════════════
#  API - 可用性数据（公开）
# ════════════════════════════════════════════

@app.route("/api/stats")
def api_stats():
    start_str = request.args.get("start","")
    end_str   = request.args.get("end","")
    if not (start_str and end_str):
        return jsonify({"error": "缺少参数"}), 400
    with get_db() as conn:
        systems = conn.execute("""
            SELECT id,name,type FROM systems
            WHERE id>0 AND enabled=1
            ORDER BY sort_order ASC, id ASC
        """).fetchall()
        normal = down = degraded = 0
        result = []
        for s in systems:
            rows = conn.execute("""
                SELECT status,is_core FROM availability_records
                WHERE system_id=? AND check_time>=? AND check_time<=?
            """, (s["id"], start_str, end_str)).fetchall()
            all_s  = calc_availability(rows)
            core_s = calc_availability([r for r in rows if r["is_core"]==1])
            latest = conn.execute("""
                SELECT status,response_ms,check_time
                FROM availability_records WHERE system_id=?
                ORDER BY check_time DESC LIMIT 1
            """, (s["id"],)).fetchone()
            cur = latest["status"] if latest else "UNKNOWN"
            if cur=="UP": normal+=1
            elif cur=="DOWN": down+=1
            elif cur=="DEGRADED": degraded+=1
            open_inc = conn.execute("""
                SELECT start_time FROM availability_incidents
                WHERE system_id=? AND end_time IS NULL LIMIT 1
            """, (s["id"],)).fetchone()
            result.append({
                "id": s["id"], "name": s["name"], "type": s["type"],
                "current_status": cur,
                "current_ms":     latest["response_ms"] if latest else 0,
                "last_check":     latest["check_time"] if latest else "",
                "avail_all":      all_s["availability"],
                "avail_core":     core_s["availability"],
                "total_checks":   all_s["total"],
                "incident_since": open_inc["start_time"] if open_inc else None,
            })
    return jsonify({"systems": result, "normal_count": normal,
                    "down_count": down, "degraded_count": degraded,
                    "total_count": len(result)})


@app.route("/api/availability")
def api_availability():
    try:
        sid       = int(request.args.get("system_id", 0))
        start_str = request.args.get("start","")
        end_str   = request.args.get("end","")
        if not all([sid, start_str, end_str]):
            return jsonify({"error": "缺少参数"}), 400
        start = parse_dt(start_str)
        end   = parse_dt(end_str)
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    gran = get_granularity(start, end)
    with get_db() as conn:
        rows = conn.execute("""
            SELECT check_time,status,response_ms,is_core
            FROM availability_records
            WHERE system_id=? AND check_time>=? AND check_time<=?
            ORDER BY check_time ASC
        """, (sid, start_str, end_str)).fetchall()
        sec_info = conn.execute(
            "SELECT url_secondary FROM systems WHERE id=?", (sid,)
        ).fetchone()

    buckets = {}
    for row in rows:
        dt   = parse_dt(row["check_time"])
        slot = dt.replace(
            minute=(dt.minute // gran) * gran if gran < 60 else 0,
            second=0, microsecond=0
        )
        if gran >= 60:
            slot = slot.replace(hour=(dt.hour // (gran//60)) * (gran//60))
        key = slot.strftime("%Y-%m-%d %H:%M:%S")
        if key not in buckets:
            buckets[key] = {"up":0,"down":0,"degraded":0,"ms":[],"is_core":row["is_core"]}
        b = buckets[key]
        st = row["status"].lower()
        b[st] = b.get(st, 0) + 1
        if row["response_ms"] > 0:
            b["ms"].append(row["response_ms"])

    timeline = []
    for key in sorted(buckets.keys()):
        b     = buckets[key]
        total = b["up"] + b["down"] + b.get("degraded",0)
        if not total:          agg = "NODATA"
        elif b["down"] >= total*0.5: agg = "DOWN"
        elif not b["up"] and b.get("degraded",0): agg = "DEGRADED"
        elif b["down"] or b.get("degraded",0):    agg = "PARTIAL"
        else:                  agg = "UP"
        avg_ms = int(sum(b["ms"])/len(b["ms"])) if b["ms"] else 0
        timeline.append({"time":key,"status":agg,"avg_ms":avg_ms,
                         "up":b["up"],"down":b["down"],
                         "degraded":b.get("degraded",0),"is_core":b["is_core"]})

    rows_list  = list(rows)
    stats_all  = calc_availability(rows_list)
    stats_core = calc_availability([r for r in rows_list if r["is_core"]==1])

    sec_timeline = []
    if sec_info and sec_info["url_secondary"]:
        with get_db() as conn:
            sec_rows = conn.execute("""
                SELECT check_time,status,response_ms
                FROM availability_records
                WHERE system_id=? AND check_time>=? AND check_time<=?
                ORDER BY check_time ASC
            """, (-sid, start_str, end_str)).fetchall()
        sec_timeline = [{"time":r["check_time"],"status":r["status"],
                         "avg_ms":r["response_ms"]} for r in sec_rows]

    return jsonify({"system_id":sid,"granularity":gran,"timeline":timeline,
                    "stats_all":stats_all,"stats_core":stats_core,
                    "sec_timeline":sec_timeline})


@app.route("/api/incidents")
def api_incidents():
    sid       = request.args.get("system_id", type=int)
    start_str = request.args.get("start","")
    end_str   = request.args.get("end","")
    only_open = request.args.get("open_only","0") == "1"

    query  = """
        SELECT i.id,i.system_id,s.name AS system_name,
               i.start_time,i.end_time,i.duration_seconds,i.error_msg,i.note
        FROM availability_incidents i
        JOIN systems s ON s.id=i.system_id WHERE 1=1
    """
    params = []
    if sid:
        query += " AND i.system_id=?"; params.append(sid)
    if start_str:
        query += " AND i.start_time>=?"; params.append(start_str)
    if end_str:
        query += " AND (i.start_time<=? OR i.end_time IS NULL)"; params.append(end_str)
    if only_open:
        query += " AND i.end_time IS NULL"
    query += " ORDER BY i.start_time DESC LIMIT 300"

    with get_db() as conn:
        rows = conn.execute(query, params).fetchall()
    result = []
    for r in rows:
        dur = r["duration_seconds"] or 0
        if r["end_time"] is None:
            try:
                dur = int((datetime.datetime.now() -
                           parse_dt(r["start_time"])).total_seconds())
            except Exception:
                dur = 0
        result.append({
            "id": r["id"], "system_id": r["system_id"],
            "system_name": r["system_name"],
            "start_time": r["start_time"], "end_time": r["end_time"],
            "duration_seconds": dur, "duration_str": fmt_duration(dur),
            "error_msg": r["error_msg"], "note": r["note"] or "",
            "is_open": r["end_time"] is None,
        })
    return jsonify(result)


@app.route("/api/incidents/<int:inc_id>", methods=["PATCH"])
@login_required
def api_update_incident(inc_id):
    data = request.get_json()
    note = data.get("note","") if data else ""
    with get_db() as conn:
        conn.execute("UPDATE availability_incidents SET note=? WHERE id=?", (note, inc_id))
        conn.commit()
    return jsonify({"ok": True})


@app.route("/api/history_summary/<int:sid>")
def api_history_summary(sid):
    now   = datetime.datetime.now()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    ranges = {
        "今天":    (today, now),
        "昨天":    (today-datetime.timedelta(days=1), today),
        "本周":    (today-datetime.timedelta(days=today.weekday()), now),
        "本月":    (today.replace(day=1), now),
        "最近7天": (now-datetime.timedelta(days=7), now),
        "最近30天":(now-datetime.timedelta(days=30), now),
    }
    result = {}
    with get_db() as conn:
        for label, (s, e) in ranges.items():
            rows = conn.execute("""
                SELECT status,is_core FROM availability_records
                WHERE system_id=? AND check_time>=? AND check_time<=?
            """, (sid, s.strftime("%Y-%m-%d %H:%M:%S"),
                  e.strftime("%Y-%m-%d %H:%M:%S"))).fetchall()
            a  = calc_availability(rows)
            ca = calc_availability([r for r in rows if r["is_core"]==1])
            incs = conn.execute("""
                SELECT duration_seconds FROM availability_incidents
                WHERE system_id=? AND start_time>=? AND start_time<=?
            """, (sid, s.strftime("%Y-%m-%d %H:%M:%S"),
                  e.strftime("%Y-%m-%d %H:%M:%S"))).fetchall()
            total_down = sum(i["duration_seconds"] or 0 for i in incs)
            result[label] = {
                "avail_all":  a["availability"],
                "avail_core": ca["availability"],
                "total_checks": a["total"],
                "down_seconds": total_down,
                "down_str":   fmt_duration(total_down),
            }
    return jsonify(result)


# ════════════════════════════════════════════
#  API - 通知配置
# ════════════════════════════════════════════

@app.route("/api/notify/config", methods=["GET"])
@login_required
def api_get_notify_config():
    with get_db() as conn:
        rows  = conn.execute(
            "SELECT channel,enabled,config_json FROM notify_config"
        ).fetchall()
        rules = conn.execute(
            "SELECT * FROM notify_rules WHERE id=1"
        ).fetchone()
    channels = {}
    for r in rows:
        try:
            cfg = json.loads(r["config_json"] or "{}")
            # 密码脱敏
            if "password" in cfg:
                cfg["password"] = "••••••••" if cfg["password"] else ""
            channels[r["channel"]] = {"enabled": bool(r["enabled"]), "config": cfg}
        except Exception:
            pass
    return jsonify({
        "channels": channels,
        "rules": dict(rules) if rules else {}
    })


@app.route("/api/notify/config", methods=["POST"])
@login_required
def api_save_notify_config():
    data     = request.get_json()
    channels = data.get("channels", {})
    rules    = data.get("rules", {})

    with get_db() as conn:
        for channel, ch_data in channels.items():
            enabled = 1 if ch_data.get("enabled") else 0
            cfg     = ch_data.get("config", {})

            # 密码：占位符则保留原密码
            if cfg.get("password") == "••••••••":
                old = conn.execute(
                    "SELECT config_json FROM notify_config WHERE channel=?",
                    (channel,)
                ).fetchone()
                if old:
                    try:
                        old_cfg = json.loads(old["config_json"] or "{}")
                        cfg["password"] = old_cfg.get("password", "")
                    except Exception:
                        pass

            conn.execute("""
                INSERT INTO notify_config (channel,enabled,config_json,updated_at)
                VALUES (?,?,?,datetime('now','localtime'))
                ON CONFLICT(channel) DO UPDATE SET
                    enabled=excluded.enabled,
                    config_json=excluded.config_json,
                    updated_at=excluded.updated_at
            """, (channel, enabled, json.dumps(cfg, ensure_ascii=False)))

        if rules:
            conn.execute("""
                INSERT INTO notify_rules
                (id,notify_on_down,notify_on_degraded,notify_on_recover,cooldown_minutes,updated_at)
                VALUES (1,?,?,?,?,datetime('now','localtime'))
                ON CONFLICT(id) DO UPDATE SET
                    notify_on_down=excluded.notify_on_down,
                    notify_on_degraded=excluded.notify_on_degraded,
                    notify_on_recover=excluded.notify_on_recover,
                    cooldown_minutes=excluded.cooldown_minutes,
                    updated_at=excluded.updated_at
            """, (1 if rules.get("notify_on_down") else 0,
                  1 if rules.get("notify_on_degraded") else 0,
                  1 if rules.get("notify_on_recover") else 0,
                  int(rules.get("cooldown_minutes", 30))))
        conn.commit()
    return jsonify({"ok": True})


@app.route("/api/notify/test", methods=["POST"])
@login_required
def api_test_notify():
    """发送测试通知"""
    data    = request.get_json()
    channel = data.get("channel","")
    cfg     = data.get("config", {})
    if not channel:
        return jsonify({"ok": False, "error": "缺少 channel"})

    # 如果密码是占位符，取数据库里的
    if cfg.get("password") == "••••••••":
        with get_db() as conn:
            old = conn.execute(
                "SELECT config_json FROM notify_config WHERE channel=?", (channel,)
            ).fetchone()
            if old:
                try:
                    old_cfg = json.loads(old["config_json"] or "{}")
                    cfg["password"] = old_cfg.get("password","")
                except Exception:
                    pass

    test_msg = {
        "title": "✅ 【测试通知】监控平台连接测试",
        "body":  "这是一条测试消息，如果您收到此消息，说明通知配置正确。\n系统可用性监控平台",
        "html_body": "<h3>✅ 测试通知</h3><p>这是一条测试消息，说明通知配置正确。</p><p><b>系统可用性监控平台</b></p>"
    }

    sys.path.insert(0, BASE_DIR)
    try:
        from monitor_probe import (send_email, send_wecom, send_dingtalk,
                                   send_feishu, send_webhook)
        senders = {"email":send_email,"wecom":send_wecom,
                   "dingtalk":send_dingtalk,"feishu":send_feishu,
                   "webhook":send_webhook}
        sender = senders.get(channel)
        if not sender:
            return jsonify({"ok": False, "error": f"未知渠道: {channel}"})
        ok, err = sender(cfg, test_msg)
        return jsonify({"ok": ok, "error": err})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:200]})


# ════════════════════════════════════════════
#  启动
# ════════════════════════════════════════════

if __name__ == "__main__":
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    if not os.path.exists(PASSWD_FILE):
        save_credentials("admin", "admin")
        print("[monitor] 首次启动，默认账号 admin/admin")
    init_db()
    print(f"[monitor] 启动在 http://0.0.0.0:{PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=False)
