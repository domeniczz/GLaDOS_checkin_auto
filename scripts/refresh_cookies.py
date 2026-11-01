#!/usr/bin/env python3
"""
GLaDOS 双账号 Cookie 刷新工具

两条路径：
  1) 手动 Cookie（推荐）：浏览器登录 glados.cloud / railgun.info 后，从一次成功的
     /api/user/status 请求里复制完整 Cookie（必须含 gld:sess 与 gld:sess.sig）与同浏览器
     User-Agent，脚本校验通过后写入 GitHub Secrets/Variables 并触发一次验证运行。
  2) 邮箱 OTP（备用）：GLaDOS 已在 /api/authorization 上启用 Turnstile，纯 API 发码会返回
     captcha_required，脚本会明确报错而不是假报成功。

Zero external dependencies (uses standard library urllib, json, subprocess)
"""

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

# 强制使用 IPv4 连接，避免双栈网络下 AAAA 记录解析后的 IPv6 握手黑洞/超时
_orig_getaddrinfo = socket.getaddrinfo


def _ipv4_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    return _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)


socket.getaddrinfo = _ipv4_getaddrinfo
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Origin": "https://glados.cloud",
    "Referer": "https://glados.cloud/login",
    "Content-Type": "application/json;charset=UTF-8",
}

DEFAULT_REPO = "yaping-pro/GLaDOS_checkin_auto"
DEFAULT_EMAILS = ["mayapingpro@gmail.com", "histevenma@gmail.com"]

# 官方入口已迁移到 glados.cloud；glados.rocks 为旧入口；railgun.info 为账号迁移后的新站
DOMAINS = ["https://glados.cloud", "https://glados.rocks", "https://railgun.info"]

CURRENT_SESSION_COOKIES = ("gld:sess", "gld:sess.sig")
LEGACY_SESSION_COOKIES = ("koa:sess", "koa:sess.sig")


def normalize_cookie(raw: str) -> str:
    """去掉 'Cookie:' 前缀与多余空白，保留完整键值对。"""
    raw = re.sub(r"^cookie\s*:\s*", "", (raw or "").strip(), flags=re.IGNORECASE)
    return re.sub(r"\s*;\s*", "; ", raw)


def cookie_kind(cookie: str):
    names = set()
    for item in cookie.split(";"):
        name, sep, _ = item.strip().partition("=")
        if sep and name:
            names.add(name)
    if set(CURRENT_SESSION_COOKIES).issubset(names):
        return "gld"
    if set(LEGACY_SESSION_COOKIES).issubset(names):
        return "koa"
    return None


def request_json(url: str, method: str = "GET", payload=None, cookie: str = "", user_agent: str = "") -> dict:
    headers = dict(HEADERS)
    if user_agent:
        headers["User-Agent"] = user_agent
    if cookie:
        headers["Cookie"] = cookie
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def validate_cookie(cookie: str, user_agent: str = "") -> dict:
    """按域名顺序验证 Cookie，返回 {ok, domain, email, leftDays, evidence}。"""
    evidence = []
    for domain in DOMAINS:
        try:
            body = request_json(f"{domain}/api/user/status", cookie=cookie, user_agent=user_agent)
        except Exception as exc:
            evidence.append(f"{domain} 请求异常 {type(exc).__name__}")
            continue
        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        if body.get("code") == 0 and data.get("email"):
            return {
                "ok": True,
                "domain": domain,
                "email": data.get("email", ""),
                "leftDays": int(float(data.get("leftDays", 0) or 0)),
                "evidence": evidence,
            }
        evidence.append(f"{domain} code={body.get('code')} {body.get('message', '')}".strip())
    return {"ok": False, "domain": "", "email": "", "leftDays": 0, "evidence": evidence}


def request_code(email: str) -> bool:
    """向指定邮箱发送登录 Access Code（注意：GLaDOS 现已启用 Turnstile）。"""
    url = "https://glados.rocks/api/authorization"
    payload = json.dumps({"address": email.strip(), "site": "glados.network"}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers=HEADERS, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        print(f"❌ [{email}] 请求异常: {exc}")
        return False

    if data.get("captcha_required"):
        print(f"❌ [{email}] 被 Turnstile 拦截（captcha_required=true），验证码未发送。")
        print("   → 请在浏览器手动登录后使用 --cookie1/--cookie2 模式。")
        return False
    if data.get("code") == 0:
        print(f"✅ [{email}] 验证码邮件已发出（通道：{data.get('method', 'sendgrid')}）")
        return True
    print(f"❌ [{email}] 发送失败: {data}")
    return False


def login_with_code(email: str, code: str) -> str:
    """使用邮箱和验证码登录，提取 gld:sess 或 koa:sess Cookie。"""
    url = "https://glados.rocks/api/login"
    payload = json.dumps(
        {
            "method": "email",
            "site": "glados.network",
            "email": email.strip().lower(),
            "mailcode": code.strip(),
        }
    ).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers=HEADERS, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("code") != 0:
                message = data.get("message", "未知错误")
                if "migrated" in message.lower():
                    message += "（账号已迁移到 railgun.info，请改用手动 Cookie 模式）"
                print(f"❌ [{email}] 登录失败: {message}")
                return ""
            set_cookies = resp.headers.get_all("Set-Cookie") or []
            cookie_dict = {}
            for item in set_cookies:
                key, _, value = item.split(";")[0].partition("=")
                if key.strip() in ("gld:sess", "gld:sess.sig", "koa:sess", "koa:sess.sig"):
                    cookie_dict[key.strip()] = value.strip()
            for prefix in ("gld", "koa"):
                sess, sig = f"{prefix}:sess", f"{prefix}:sess.sig"
                if sess in cookie_dict and sig in cookie_dict:
                    cookie_str = f"{sess}={cookie_dict[sess]}; {sig}={cookie_dict[sig]}"
                    print(f"🎉 [{email}] 登录成功，已提取 {prefix}:sess Cookie")
                    return cookie_str
            print(f"⚠️ [{email}] 登录成功但未提取到完整 Session Cookie: {set_cookies}")
            return ""
    except Exception as exc:
        print(f"❌ [{email}] 登录异常: {exc}")
        return ""


def get_account_status(cookie: str) -> dict:
    status = validate_cookie(cookie)
    return status


def exchange_points(cookie: str, plan_type: str = "plan200") -> bool:
    """兑换积分: plan100(100分=10天), plan200(200分=30天), plan500(500分=100天)"""
    url = "https://glados.rocks/api/user/exchange"
    payload = json.dumps({"planType": plan_type}).encode("utf-8")
    headers = {**HEADERS, "Cookie": cookie}
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("code") == 0:
            print(f"🎉 积分兑换成功 [{plan_type}]: {data.get('message', '兑换成功')} (当前结余: {data.get('points')} 分)")
            return True
        print(f"⚠️ 兑换失败: {data.get('message', '未知错误')}")
    except Exception as exc:
        print(f"❌ 兑换异常: {exc}")
    return False


def update_github(combined_cookie: str, user_agent: str, repo: str = DEFAULT_REPO) -> bool:
    """写入 Secret + 轮换日期/UA 变量，并触发一次验证运行。"""
    try:
        subprocess.run(
            ["gh", "secret", "set", "GLADOS_COOKIE", "--repo", repo],
            input=combined_cookie.encode("utf-8"),
            check=True,
            capture_output=True,
        )
        print(f"✅ GitHub Secret [GLADOS_COOKIE] 更新成功！({repo})")
    except subprocess.CalledProcessError as exc:
        print(f"❌ GitHub Secret 写入失败: {exc.stderr.decode('utf-8')}")
        return False

    today = time.strftime("%Y-%m-%d")
    for name, value in (("GLADOS_COOKIE_ROTATED_AT", today), ("GLADOS_USER_AGENT", user_agent)):
        if not value:
            continue
        try:
            subprocess.run(
                ["gh", "variable", "set", name, "--repo", repo, "--body", value],
                check=True,
                capture_output=True,
            )
            print(f"✅ 已写入变量 {name}")
        except subprocess.CalledProcessError as exc:
            print(f"⚠️ 写入变量 {name} 失败（不影响签到）: {exc.stderr.decode('utf-8')}")

    try:
        subprocess.run(
            ["gh", "workflow", "run", "runGladosAction.yml", "--repo", repo],
            check=True,
            capture_output=True,
        )
        print("🚀 已触发 GitHub Actions 验证运行")
    except subprocess.CalledProcessError as exc:
        print(f"⚠️ 触发工作流失败: {exc.stderr.decode('utf-8')}")
    return True


def manual_mode(args) -> int:
    cookies = []
    for label, raw in (("账号1", args.cookie1), ("账号2", args.cookie2)):
        if not raw:
            continue
        cookie = normalize_cookie(raw)
        kind = cookie_kind(cookie)
        if kind != "gld":
            print(f"⚠️ {label} Cookie 未包含完整 gld:sess + gld:sess.sig（当前: {kind or '未知'}），新版接口可能返回 -2。")
        result = validate_cookie(cookie, args.user_agent)
        if not result["ok"]:
            print(f"❌ {label} 校验失败: {'; '.join(result['evidence'][-3:])}")
            return 1
        print(f"✅ {label} 校验通过：{result['email']} | 剩余 {result['leftDays']} 天 | 域名 {result['domain']}")
        cookies.append(cookie)

    if not cookies:
        print("❌ 未提供任何 Cookie")
        return 1

    print("\n>>> 写入 GitHub Secrets / Variables ...")
    if not update_github("&".join(cookies), args.user_agent):
        return 1
    print("\n🎉 完成。手动登录后无需再跑其它命令。")
    return 0


def otp_mode(args) -> int:
    if args.send_only:
        print(">>> 正在向双账号发送 Access Code...")
        ok = all(request_code(email) for email in DEFAULT_EMAILS)
        print(">>> 发送完成，请查收邮件并提供 6 位验证码。")
        return 0 if ok else 1

    code1, code2 = args.account1_code, args.account2_code
    if not code1 or not code2:
        print("=== GLaDOS OTP 刷新（备用路径）===")
        print("若未收到验证码，请先运行 `python3 scripts/refresh_cookies.py --send-only`。")
        if not code1:
            code1 = input(f"请输入 [{DEFAULT_EMAILS[0]}] 邮箱收到的 6 位验证码: ").strip()
        if not code2:
            code2 = input(f"请输入 [{DEFAULT_EMAILS[1]}] 邮箱收到的 6 位验证码: ").strip()
    if not code1 or not code2:
        print("❌ 验证码不完整，已终止操作。")
        return 1

    ck1 = login_with_code(DEFAULT_EMAILS[0], code1)
    if not ck1:
        return 1
    status1 = get_account_status(ck1)
    if not status1["ok"]:
        print(f"❌ 账号 1 登录后校验失败: {'; '.join(status1['evidence'][-3:])}")
        return 1
    print(f"📊 账号 1: {status1['email']} | 剩余 {status1['leftDays']} 天")

    if args.auto_exchange and status1["leftDays"] <= 7:
        print("⚡ 账号 1 临期，尝试【200 积分 = 30 天】兑换...")
        exchange_points(ck1, "plan200")

    ck2 = login_with_code(DEFAULT_EMAILS[1], code2)
    if not ck2:
        return 1
    status2 = get_account_status(ck2)
    if not status2["ok"]:
        print(f"❌ 账号 2 登录后校验失败: {'; '.join(status2['evidence'][-3:])}")
        return 1
    print(f"📊 账号 2: {status2['email']} | 剩余 {status2['leftDays']} 天")

    return 0 if update_github(f"{ck1}&{ck2}", args.user_agent) else 1


def main():
    parser = argparse.ArgumentParser(description="GLaDOS Cookie 刷新（手动 Cookie 优先，OTP 备用）")
    parser.add_argument("--repo", default=DEFAULT_REPO, help="GitHub 仓库路径")
    parser.add_argument("--cookie1", help="账号1 的完整 Cookie 请求头（含 gld:sess 与 gld:sess.sig）")
    parser.add_argument("--cookie2", help="账号2 的完整 Cookie 请求头")
    parser.add_argument("--user-agent", default=os.environ.get("GLADOS_USER_AGENT", ""), help="生成 Cookie 的浏览器 User-Agent")
    parser.add_argument("--send-only", action="store_true", help="仅触发向预设邮箱发送 Access Code")
    parser.add_argument("--account1-code", help="账号1 (mayapingpro) 的验证码")
    parser.add_argument("--account2-code", help="账号2 (histevenma) 的验证码")
    parser.add_argument("--auto-exchange", action="store_true", default=True, help="临期且积分充足时自动兑换")
    args = parser.parse_args()

    if args.cookie1 or args.cookie2:
        return manual_mode(args)
    return otp_mode(args)


if __name__ == "__main__":
    sys.exit(main())
