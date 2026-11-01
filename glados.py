import base64
import hashlib
import hmac
import json
import os
import re
import socket
import sys
import time
from urllib.parse import urlparse

import requests

# -------------------------------------------------------------------------------------------
# 强制 Python 全局使用 IPv4 连接
# 解决 GitHub Actions / Azure Ubuntu 24.04 双栈 Runner 解析 AAAA 记录后 IPv6 握手黑洞、
# 导致 requests 对 glados.cloud / glados.rocks 出现 20s ReadTimeout 的根本问题。
# -------------------------------------------------------------------------------------------
_orig_getaddrinfo = socket.getaddrinfo


def _ipv4_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    return _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)


socket.getaddrinfo = _ipv4_getaddrinfo

# -------------------------------------------------------------------------------------------
# 社区方案移植（2026-10，参考 lankerr/2026-glados-checkin 与 Devilstore/Glados-Railgun-checkin）
#   1. 官方已迁移到 glados.cloud；glados.rocks 为旧入口，railgun.info 为账号迁移后的新站
#   2. 签到 payload 的 token 必须等于当前域名（旧的 glados.one 会触发 Automated check-in detected）
#   3. 新版会话为 gld:sess + gld:sess.sig；旧 koa:sess 在新接口可能直接 -2
#   4. 服务端做设备绑定：UA 必须与生成会话的浏览器一致，否则 code=4 / device-mismatch
# -------------------------------------------------------------------------------------------

DOMAINS = ["https://glados.cloud", "https://glados.rocks", "https://railgun.info"]

# 签到 token 默认等于域名；railgun 站点参数为 railgun.one
DOMAIN_TOKENS = {"railgun.info": "railgun.one"}

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

# 设备绑定失配时按服务端 loginDevice 切换 UA 重试
PLATFORM_UA = {
    "Windows": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "macOS": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Linux": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "iPhone": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1",
    "Android": "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
}

CURRENT_SESSION_COOKIES = ("gld:sess", "gld:sess.sig")
LEGACY_SESSION_COOKIES = ("koa:sess", "koa:sess.sig")

NORMAL_CHECKIN_MARKERS = (
    "checkin! got",
    "checkin repeats! please try tomorrow",
    "today's observation logged",
)


def log(msg):
    print(msg, flush=True)


# -------------------------------------------------------------------------------------------
# Cookie 解析
# -------------------------------------------------------------------------------------------
def cookie_names(cookie_header):
    names = set()
    for item in cookie_header.split(";"):
        name, sep, _ = item.strip().partition("=")
        if sep and name:
            names.add(name)
    return names


def cookie_kind(cookie_header):
    names = cookie_names(cookie_header)
    if set(CURRENT_SESSION_COOKIES).issubset(names):
        return "gld"
    if set(LEGACY_SESSION_COOKIES).issubset(names):
        return "koa"
    return None


def extract_cookie(raw):
    """支持 Cookie 请求头、Cookie-Editor JSON 导出、纯 JWT。"""
    if not raw:
        return None
    raw = re.sub(r"^cookie\s*:\s*", "", raw.strip(), flags=re.IGNORECASE)
    if any(f"{name}=" in raw for name in CURRENT_SESSION_COOKIES + LEGACY_SESSION_COOKIES):
        return raw
    if raw.startswith(("{", "[")):
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
        if isinstance(parsed, list):
            pairs = [
                f"{item['name']}={item['value']}"
                for item in parsed
                if isinstance(item, dict) and item.get("name") and item.get("value") is not None
            ]
            joined = "; ".join(pairs)
            return joined if joined and cookie_kind(joined) else None
        token = parsed.get("token") if isinstance(parsed, dict) else None
        return f"koa:sess={token}" if token else None
    if raw.count(".") == 2 and "=" not in raw and len(raw) > 50:
        return "koa:sess=" + raw
    return raw


def parse_cookies(raw):
    raw = (raw or "").strip()
    if not raw:
        return []
    if raw.lstrip().startswith(("{", "[")):
        cookie = extract_cookie(raw)
        return [cookie] if cookie else []
    sep = "\n" if "\n" in raw else "&"
    return [cookie for part in raw.split(sep) if (cookie := extract_cookie(part))]


# -------------------------------------------------------------------------------------------
# HTTP 客户端
# -------------------------------------------------------------------------------------------
def browser_headers(user_agent):
    headers = {
        "User-Agent": user_agent,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    match = re.search(r"(?:Chrome|Chromium)/(\d+)", user_agent)
    if match:
        major = match.group(1)
        if "Macintosh" in user_agent:
            platform = "macOS"
        elif "Windows" in user_agent:
            platform = "Windows"
        elif "Android" in user_agent:
            platform = "Android"
        elif "Linux" in user_agent:
            platform = "Linux"
        else:
            platform = "Unknown"
        headers.update(
            {
                "Sec-CH-UA": f'"Chromium";v="{major}", "Google Chrome";v="{major}", "Not_A Brand";v="99"',
                "Sec-CH-UA-Mobile": "?1" if "Mobile" in user_agent else "?0",
                "Sec-CH-UA-Platform": f'"{platform}"',
            }
        )
    return headers


def domain_token(domain):
    host = urlparse(domain).hostname or domain
    return DOMAIN_TOKENS.get(host, host)


class GladosClient:
    def __init__(self, cookie, user_agent=DEFAULT_USER_AGENT, domains=None):
        self.cookie = cookie
        self.user_agent = user_agent
        self.domains = list(domains or DOMAINS)
        self.domain = None
        self.evidence = []

    def _order(self):
        if self.domain and self.domain in self.domains:
            return [self.domain] + [d for d in self.domains if d != self.domain]
        return list(self.domains)

    def _send(self, domain, method, path, payload=None, form=False, user_agent=None):
        headers = browser_headers(user_agent or self.user_agent)
        headers.update(
            {
                "Cookie": self.cookie,
                "Origin": domain,
                "Referer": f"{domain}/console/checkin",
            }
        )
        try:
            if form:
                resp = requests.post(f"{domain}{path}", headers=headers, data=payload, timeout=20)
            elif method == "GET":
                resp = requests.get(f"{domain}{path}", headers=headers, timeout=20)
            else:
                resp = requests.post(f"{domain}{path}", headers=headers, json=payload, timeout=20)
        except requests.RequestException as exc:
            self.evidence.append(f"{domain} 网络异常 {type(exc).__name__}")
            return None
        if resp.status_code != 200:
            self.evidence.append(f"{domain} HTTP {resp.status_code}")
            return None
        try:
            return resp.json()
        except ValueError:
            self.evidence.append(f"{domain} 返回非 JSON")
            return None

    def status(self, attempts=3, delay=10):
        """返回 (是否已认证, 状态数据)。按域名顺序回退，只有 code=0 且带 email 才算成功。遇到网络异常支持重试。"""
        last = None
        for attempt in range(1, max(1, attempts) + 1):
            has_network_err = False
            for domain in self._order():
                body = self._send(domain, "GET", "/api/user/status")
                if body is None:
                    has_network_err = True
                    continue
                last = body
                data = body.get("data") if isinstance(body.get("data"), dict) else {}
                if body.get("code") == 0 and data.get("email"):
                    self.domain = domain
                    return True, data
                self.evidence.append(f"{domain} code={body.get('code')} {body.get('message', '')}".strip())
            # 如果全是明确的业务拒绝 (如 code=-2)，不再重复重试
            if not has_network_err:
                break
            if attempt < attempts:
                log(f"⚠️ 账号状态查询第 {attempt}/{attempts} 次遇网络异常，{delay} 秒后重试")
                time.sleep(max(0, delay))
        return False, (last or {})

    def points(self):
        if not self.domain:
            return 0
        body = self._send(self.domain, "GET", "/api/user/points")
        if not body:
            return 0
        try:
            return int(float(body.get("points", 0)))
        except (TypeError, ValueError):
            return 0

    def checkin(self, attempts=3, delay=60):
        """签到。认证/设备错误不重试，网络与未知错误按次数退避重试。"""
        last = None
        for attempt in range(1, max(1, attempts) + 1):
            for domain in self._order():
                payload = {"token": domain_token(domain)}
                body = self._send(domain, "POST", "/api/user/checkin", payload)
                if body is None:
                    continue

                # 设备绑定失配：用服务端声明的平台 UA 重试一次
                if body.get("code") == 4 and body.get("reason") == "device-mismatch":
                    device = body.get("loginDevice")
                    recover_ua = PLATFORM_UA.get(device)
                    if recover_ua and recover_ua != self.user_agent:
                        log(f"⚠️ {domain} 设备平台不匹配 (loginDevice={device})，切换 UA 重试")
                        retry = self._send(domain, "POST", "/api/user/checkin", payload, user_agent=recover_ua)
                        if retry is not None:
                            body = retry

                last = body
                if is_normal_checkin(body) or is_non_retryable(body):
                    self.domain = domain
                    return domain, body
                self.evidence.append(f"{domain} 签到 code={body.get('code')} {body.get('message', '')}".strip())
            if attempt < attempts:
                log(f"⚠️ 签到第 {attempt}/{attempts} 次未成功，{delay} 秒后重试")
                time.sleep(max(0, delay))
        return self.domain, last

    def exchange(self, plan):
        """兑换：优先表单（社区实测新接口要求），失败再退回 JSON。"""
        if not self.domain:
            return None
        body = self._send(self.domain, "POST", "/api/user/exchange", {"planType": plan}, form=True)
        if body is not None and body.get("code") == 0:
            return body
        fallback = self._send(self.domain, "POST", "/api/user/exchange", {"planType": plan})
        return fallback if fallback is not None else body


def is_normal_checkin(result):
    if not isinstance(result, dict):
        return False
    message = str(result.get("message", "")).strip().lower()
    if any(marker in message for marker in NORMAL_CHECKIN_MARKERS):
        return True
    return result.get("code") == 0


def is_non_retryable(result):
    if not isinstance(result, dict):
        return False
    code = result.get("code")
    reason = str(result.get("reason", "")).strip().lower()
    message = str(result.get("message", "")).strip().lower()
    return (
        code == -2
        or reason == "device-mismatch"
        or "没有权限" in message
        or "permission" in message
        or "unauthorized" in message
        or "automated check-in detected" in message
    )


# -------------------------------------------------------------------------------------------
# 通知
# -------------------------------------------------------------------------------------------
def send_telegram(title, content, token=""):
    """Telegram 主通道：按 4000 字符切分，纯文本发送，避免 HTML 解析失败。

    token 缺省回退 TG_BOT_TOKEN；调用方按严重度选择 alerts（P1）或 digest（P2）。
    """
    token = token or os.environ.get("TG_BOT_TOKEN", "")
    chat = os.environ.get("TG_CHAT_ID", "")
    api = os.environ.get("TG_API_HOST", "https://tg.yaping.dpdns.org").rstrip("/")
    if not token or not chat:
        return
    text = f"{title}\n\n{content.strip()}"
    chunks, cur = [], ""
    for line in text.splitlines(keepends=True):
        if cur and len(cur) + len(line) > 4000:
            chunks.append(cur)
            cur = line
        else:
            cur += line
    if cur:
        chunks.append(cur)
    for i, chunk in enumerate(chunks, 1):
        prefix = f"({i}/{len(chunks)})\n" if len(chunks) > 1 else ""
        try:
            res = requests.post(
                f"{api}/bot{token}/sendMessage",
                data={"chat_id": chat, "text": prefix + chunk},
                timeout=15,
            )
            log(f"Telegram 推送响应[{i}/{len(chunks)}]: {res.text[:200]}")
        except Exception as exc:
            log(f"Telegram 推送失败: {exc}")


def send_feishu(title, content):
    webhook = os.environ.get("FEISHU_WEBHOOK", "")
    secret = os.environ.get("FEISHU_SECRET", "")
    if not webhook:
        return
    try:
        timestamp = str(int(time.time()))
        payload = {
            "msg_type": "post",
            "content": {
                "post": {
                    "zh_cn": {
                        "title": title,
                        "content": [[{"tag": "text", "text": content.strip()}]],
                    }
                }
            },
        }
        if secret:
            string_to_sign = f"{timestamp}\n{secret}"
            hmac_code = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
            payload["timestamp"] = timestamp
            payload["sign"] = base64.b64encode(hmac_code).decode("utf-8")
        res = requests.post(webhook, json=payload, timeout=10)
        log(f"飞书推送响应: {res.text[:200]}")
    except Exception as exc:
        log(f"飞书推送失败: {exc}")


def send_bark(title, content):
    key = os.environ.get("BARK_KEY", "")
    if not key:
        return
    try:
        requests.get(f"https://api.day.app/{key}/{title}/{content}", timeout=10)
    except Exception as exc:
        log(f"Bark 推送失败: {exc}")


# -------------------------------------------------------------------------------------------
# 主流程
# -------------------------------------------------------------------------------------------
def env_flag(name):
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "y")


def choose_exchange_plan(days_left, points):
    if days_left > 7:
        return None
    if points >= 500:
        return "plan500", 500, 100
    if points >= 200:
        return "plan200", 200, 30
    if points >= 100 and days_left <= 2:
        return "plan100", 100, 10
    return None


def diagnose(cookies, user_agent):
    log("🔎 只读诊断：仅查询登录状态，不签到、不兑换、不推送")
    failures = 0
    for idx, cookie in enumerate(cookies, 1):
        kind = cookie_kind(cookie)
        client = GladosClient(cookie, user_agent)
        ok, data = client.status(attempts=2, delay=5)
        if ok:
            log(f"✅ 账号[{idx}] 登录态有效（会话类型 {kind or '未知'}，域名 {client.domain}）")
        else:
            failures += 1
            log(f"❌ 账号[{idx}] 登录态无效（会话类型 {kind or '未知'}）：{'; '.join(client.evidence[-3:])}")
    log("只读通过不代表签到设备校验通过；实际签到仍需匹配登录浏览器 UA")
    return 1 if failures else 0


def main():
    cookies = parse_cookies(os.environ.get("GLADOS_COOKIE", ""))
    if not cookies:
        log("未获取到 COOKIE 变量")
        return 0

    user_agent = os.environ.get("GLADOS_USER_AGENT", "").strip() or DEFAULT_USER_AGENT
    if env_flag("GLADOS_DIAGNOSE_ONLY"):
        return diagnose(cookies, user_agent)

    send_content = ""
    has_warning = False
    has_cookie_expired = False
    has_network_failure = False
    attempts = int(os.environ.get("CHECKIN_MAX_ATTEMPTS", "3"))
    delay = int(os.environ.get("CHECKIN_RETRY_DELAY_SECONDS", "60"))

    for idx, cookie in enumerate(cookies, 1):
        kind = cookie_kind(cookie)
        client = GladosClient(cookie, user_agent)
        authed, data = client.status(attempts=attempts, delay=min(15, delay))

        if not authed:
            evidence = "; ".join(client.evidence[-3:]) or "无有效响应"
            # 只有在主站 (glados.cloud / glados.rocks) 明确返回 code=-2 时才判为 Cookie 失效
            # 排除由于主站超时降级到未迁移的 railgun.info 报 No permission 的误报
            is_cookie_expired = any(
                ("glados.cloud" in ev or "glados.rocks" in ev) and ("code=-2" in ev or "没有权限" in ev)
                for ev in client.evidence
            )
            if is_cookie_expired:
                has_cookie_expired = True
                log(f"账号[{idx}]----结果--Cookie已失效(鉴权失败)---- {evidence}")
                send_content += "\n".join(
                    [
                        f"🚨 账号[{idx}]：登录态已失效（会话类型 {kind or '未知'}）",
                        f"❌ 鉴权证据：{evidence}",
                        "🛠 处置：在浏览器登录 glados.cloud（旧号可能已迁移到 railgun.info），"
                        "从成功的 /api/user/status 请求复制完整 Cookie（需含 gld:sess 与 gld:sess.sig）"
                        "及同浏览器 User-Agent，执行 `python3 scripts/refresh_cookies.py --cookie1 ... --user-agent ...`。",
                    ]
                ) + "\n\n"
            else:
                has_network_failure = True
                log(f"账号[{idx}]----结果--网络/服务异常(未判失效)---- {evidence}")
                send_content += "\n".join(
                    [
                        f"⚠️ 账号[{idx}]：登录状态检查网络异常（未判失效）",
                        f"❌ 证据：{evidence}",
                        "ℹ️ 可能是 GitHub Runner 出口网络抖动，后续自动重试，无需重置 Secret。",
                    ]
                ) + "\n\n"
            continue

        email = data.get("email", "")
        try:
            days_left = int(float(data.get("leftDays", 0)))
        except (TypeError, ValueError):
            days_left = 0

        domain, checkin_body = client.checkin(attempts=attempts, delay=delay)
        checkin_body = checkin_body or {}
        message = checkin_body.get("message", "签到完成")

        if checkin_body.get("code") == 4:
            has_warning = True
            device = checkin_body.get("loginDevice", "未知")
            message = f"设备校验失败（loginDevice={device}），请用同一浏览器 UA 重新登录"

        points = client.points()
        log(f"{email}----结果--{message}----积分({points})----剩余({days_left})天----域名({domain})")

        lines = [
            f"👤 账号：{email}",
            f"🌐 域名：{domain}（会话类型 {kind or '未知'}）",
            f"📝 签到结果：{message}",
            f"💰 当前结余积分：{points} 分",
            f"⏳ 剩余服务天数：{days_left} 天",
        ]

        exchange_plan = choose_exchange_plan(days_left, points)
        exchanged = False
        if exchange_plan:
            plan_key, cost_points, add_days = exchange_plan
            result = client.exchange(plan_key) or {}
            if result.get("code") == 0:
                exchanged = True
                log(f"🎉 {email} 自动兑换成功：{cost_points} 积分 = {add_days} 天")
                try:
                    points = int(float(result.get("points", points - cost_points)))
                except (TypeError, ValueError):
                    points -= cost_points
                days_left += add_days
                lines.append(
                    f"\n🎉【已自动续期】成功兑换【{cost_points}积分 = {add_days}天】！\n"
                    f"👉 最新服务天数：{days_left} 天，剩余积分：{points} 分"
                )
            else:
                lines.append(f"\n⚠️【自动兑换失败】{result.get('message', '未知错误')}")

        if days_left <= 7 and not exchanged:
            has_warning = True
            lines.append(f"\n⚠️【到期预警】服务仅剩 {days_left} 天，请及时续期！")
            if points >= 500:
                lines.append(f"💡 当前积分充足（{points}分），推荐兑换【500积分 = 100天】！")
            elif points >= 200:
                lines.append(f"💡 当前可兑换【200积分 = 30天】进行续命过渡（剩余 {points} 分）！")
            elif points >= 100:
                lines.append(f"💡 当前可兑换【100积分 = 10天】（剩余 {points} 分）！")
            else:
                lines.append(f"⚠️ 积分仅剩 {points} 分，不足最低兑换门槛（100分），请留意服务中断！")
        else:
            if points < 200:
                lines.append(f"🎯 积分目标：距离 200 分（30天续期档）还差 {200 - points} 分")
            elif points < 500:
                lines.append(f"🎯 积分目标：已达成 200 分档！距离 500 分（永久续期档）还差 {500 - points} 分")
            else:
                lines.append(f"🎉 积分已达 {points} 分！可随时兑换 500 积分 = 100 天！")

        send_content += "\n".join(lines) + "\n\n"

    # 凭证年龄预警：会话由服务端吊销（实测寿命 14~31 天），到期前留出人工续期窗口
    rotated_at = os.environ.get("GLADOS_COOKIE_ROTATED_AT", "").strip()
    if rotated_at and not has_cookie_expired:
        try:
            age_days = int((time.time() - time.mktime(time.strptime(rotated_at, "%Y-%m-%d"))) / 86400)
        except Exception:
            age_days = -1
        if age_days >= 14:
            has_warning = True
            send_content = (
                f"🕒 凭证年龄：{age_days} 天（实测会话寿命 14~31 天）\n"
                "🛠 建议本周内重新登录并运行 `python3 scripts/refresh_cookies.py` 主动续期，避免断签。\n\n"
            ) + send_content
            log(f"凭证年龄预警: {age_days} 天")

    if has_cookie_expired:
        notice_title = "🚨 GLaDOS 签到报警：Cookie 已失效！"
    elif has_network_failure:
        notice_title = "⚠️ GLaDOS 签到网络异常通知"
    elif has_warning:
        notice_title = "⚠️ GLaDOS 续期预警通知"
    else:
        notice_title = "🎉 GLaDOS 自动签到通知"

    send_bark(notice_title, send_content)
    send_feishu(notice_title, send_content)

    # Cookie 失效属 P1 走 alerts，其余日常结果走 digest
    if has_cookie_expired:
        send_telegram(notice_title, send_content, os.environ.get("TG_ALERTS_BOT_TOKEN", ""))
    else:
        send_telegram(notice_title, send_content, os.environ.get("TG_DIGEST_BOT_TOKEN", ""))

    return 1 if (has_cookie_expired or has_network_failure) else 0


if __name__ == "__main__":
    sys.exit(main())
