#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
监控平台密码管理工具
用法:
    python3 /opt/monitor/passwd.py              # 交互式修改
    python3 /opt/monitor/passwd.py -u admin -p newpass  # 直接设置
    python3 /opt/monitor/passwd.py --show       # 显示当前账号
"""

import os, sys, argparse

PASSWD_FILE = "/opt/monitor/.passwd"


def load():
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
    except Exception as e:
        print(f"[错误] 读取失败: {e}")
    return "admin", "admin"


def save(username, password):
    content = (
        "# 监控平台登录凭据\n"
        "# 修改后重启服务生效：systemctl restart monitor\n"
        f"username={username}\n"
        f"password={password}\n"
    )
    with open(PASSWD_FILE, "w") as f:
        f.write(content)
    os.chmod(PASSWD_FILE, 0o600)
    print(f"[OK] 已保存到 {PASSWD_FILE}")
    print("[提示] 执行以下命令使新密码生效：systemctl restart monitor")


def main():
    parser = argparse.ArgumentParser(description="监控平台密码管理工具")
    parser.add_argument("-u", "--username", help="设置用户名")
    parser.add_argument("-p", "--password", help="设置密码")
    parser.add_argument("--show", action="store_true", help="显示当前用户名")
    args = parser.parse_args()

    current_user, current_pass = load()

    if args.show:
        print(f"当前用户名: {current_user}"); return

    if args.username or args.password:
        new_user = args.username or current_user
        new_pass = args.password
        if not new_pass:
            print("[错误] 请用 -p 指定密码"); sys.exit(1)
        if len(new_pass) < 4:
            print("[错误] 密码至少4位"); sys.exit(1)
        save(new_user, new_pass); return

    # 交互式
    print("=" * 40)
    print("  监控平台密码修改工具")
    print("=" * 40)
    print(f"当前用户名: {current_user}\n")
    old = input("请输入原密码: ").strip()
    if old != current_pass:
        print("[错误] 原密码错误"); sys.exit(1)
    new1 = input("请输入新密码（至少4位）: ").strip()
    if len(new1) < 4:
        print("[错误] 密码至少4位"); sys.exit(1)
    new2 = input("请再次输入新密码: ").strip()
    if new1 != new2:
        print("[错误] 两次输入不一致"); sys.exit(1)
    new_user = input(f"用户名（回车保持 {current_user} 不变）: ").strip() or current_user
    save(new_user, new1)


if __name__ == "__main__":
    main()
