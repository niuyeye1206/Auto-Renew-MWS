#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MWS (Puratya / cloud.m-ws.cc) 浏览器版自动续期

背景: 续期 API (POST /api/bots/{id}/renew) 现已返回
  HTTP 403 {"code":"captcha_required",
            "detail":"延長には画面での確認が必要です。ダッシュボードから延長してください。"}
平台要求在面板页面上确认续期, API 路线已死。本脚本改走浏览器:
  1) 注入 COOKIE (__Host-mrtcloud_token) 登录面板
  2) 打开服务器详情页, 找到 延長 / Renew 按钮并点击
  3) 如出现 Cloudflare Turnstile, 用与 Bot-Hosting 相同的套路处理
     (iframe/widget 定位 + xdotool/CDP 点击 + 轮询 cf-turnstile-response)
  4) 等待确认结果, 发 TG 通知

环境变量:
  EMAIL         通知用邮箱 (可选)
  SERVER_IDS    服务器 id, 多个用逗号分隔 (必填, 如 17469)
  COOKIE        __Host-mrtcloud_token 的值 (必填, JWT, 约 1 个月有效期)
  TG_BOT_TOKEN  Telegram Bot Token (可选, 与 TG_CHAT_ID 一起生效)
  TG_CHAT_ID    Telegram Chat ID (可选)
  IS_PROXY      是否启用代理 (由 sing-box 步骤写进 $GITHUB_ENV)
  PROXY_SERVER  代理地址, 如 socks5://127.0.0.1:1080 (由 sing-box 步骤写进 $GITHUB_ENV)
  HEADLESS      是否无头模式, 默认 true (GitHub Actions 无显示器, 必须无头)

注意:
  - 不再使用 Discord OAuth 登录 (DISCORD_TOKEN 已失效 401)。
  - COOKIE 过期后需手动更新 Secret, 脚本会明确报错。
  - 无论成功失败脚本都 exit 0 (与旧版一致, 保证后续 time.txt 提交步骤能跑),
    结果以 TG 通知为准。
"""

import os
import re
import sys
import json
import time
import base64
import random
import subprocess
import ctypes
import requests
from datetime import datetime, timezone, timedelta
from seleniumbase import SB

# ================= 环境变量配置 =================
EMAIL         = os.environ.get("EMAIL") or ""
SERVER_IDS    = os.environ.get("SERVER_IDS") or ""
COOKIE        = os.environ.get("COOKIE") or ""
TG_BOT_TOKEN  = os.environ.get("TG_BOT_TOKEN") or ""
TG_CHAT_ID    = os.environ.get("TG_CHAT_ID") or ""
IS_PROXY      = os.environ.get("IS_PROXY", "false").lower() == "true"
PROXY_SERVER  = os.environ.get("PROXY_SERVER", "").strip() or "http://127.0.0.1:1080"
HEADLESS      = os.environ.get("HEADLESS", "true").lower() == "true"

SITE_ORIGIN = "https://cloud.m-ws.cc"
COOKIE_NAME = "__Host-mrtcloud_token"

# 续期按钮文案 (按优先级排序, 日文最优先, 服务是日本站)
RENEW_KEYWORDS = ["延長", "延长", "Renew", "续期", "更新"]
# 确认弹窗按钮文案
CONFIRM_KEYWORDS = ["確認", "确认", "OK", "はい", "Yes", "確定", "确定"]
# 成功标志文案
SUCCESS_KEYWORDS = ["延長しました", "续期成功", "延長成功", "更新しました",
                    "完了しました", "extended", "renewed", "成功"]
# 登录页标志 (出现这些说明 COOKIE 失效, 被踢回登录)
LOGIN_MARKERS = ["ログイン", "Sign in", "Discord", "login"]

# ================= TG 通知 =================
def send_tg_notification(message: str):
    """发送 Telegram 通知 (沿用旧版格式)"""
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("⚠️ 未配置 TG_BOT_TOKEN 或 TG_CHAT_ID，跳过 TG 通知。")
        return
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TG_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            print("✅ TG 通知发送成功")
        else:
            print(f"❌ TG 通知发送失败: {resp.text[:200]}")
    except Exception as e:
        print(f"❌ TG 通知发送异常: {e}")

def format_notification(status: str, server_lines, extra: str = "") -> str:
    local_time = time.gmtime(time.time() + 8 * 3600)
    current_time = time.strftime("%Y-%m-%d %H:%M:%S", local_time)
    lines = ["🇯🇵 MWS 续期通知\n"]
    lines.append(status)
    lines.extend(server_lines)
    if extra:
        lines.append(extra)
    lines.append(f"👤 续期账户: {EMAIL}")
    lines.append(f"⏱️ 运行时间: {current_time}")
    return "\n".join(lines)

def mask_secret(value: str) -> str:
    if not value:
        return "***"
    return value[:4] + "..." + value[-4:] if len(value) > 8 else "***"

def get_expiry_from_cookie(value: str):
    """从 JWT 里解析过期时间 (JST 展示), 仅用于日志, 解析失败返回 None"""
    try:
        payload_b64 = value.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        exp = payload.get("exp")
        if not exp:
            return None
        return datetime.fromtimestamp(exp, tz=timezone.utc).astimezone(
            timezone(timedelta(hours=9))
        )
    except Exception:
        return None

# ================= 基础工具 (复用 Bot-Hosting 同款) =================
def js(sb, script, default=None, errors=None):
    try:
        result = sb.execute_script(script)
        return default if result is None else result
    except Exception as e:
        if errors is not None:
            errors.append(f"execute_script: {type(e).__name__}: {e}")
        try:
            result = sb.cdp.evaluate("(function(){ " + script + " })()")
            return default if result is None else result
        except Exception as e2:
            if errors is not None:
                errors.append(f"cdp.evaluate: {type(e2).__name__}: {e2}")
            return default

def diag_turnstile_dom(sb, errors=None):
    """Turnstile 定位失败时的现场诊断快照"""
    snap = js(sb, """
        var out = [];
        var ifr = document.querySelectorAll('iframe');
        out.push('top-doc iframes: ' + ifr.length);
        for (var i = 0; i < ifr.length; i++) {
            var r = ifr[i].getBoundingClientRect();
            out.push('iframe w=' + Math.round(r.width) + ' h=' + Math.round(r.height) + ' src=' + (ifr[i].src || '').slice(0, 90));
        }
        var ts = document.querySelectorAll('[class*="turnstile"], [id*="turnstile"], input[name="cf-turnstile-response"]');
        out.push('turnstile-ish elements: ' + ts.length);
        for (var j = 0; j < ts.length; j++) {
            var rr = ts[j].getBoundingClientRect();
            var cls = (typeof ts[j].className === 'string') ? ts[j].className : (ts[j].getAttribute('class') || '');
            out.push(' - ' + ts[j].tagName + ' class=' + cls + ' w=' + Math.round(rr.width) + ' h=' + Math.round(rr.height));
        }
        out.push('window.turnstile=' + (typeof window.turnstile) + ' readyState=' + document.readyState);
        return out.join('\\n');
    """, "")
    for line in str(snap).splitlines():
        print("   [diag] " + line)
    if errors:
        for err in errors:
            print("   [diag][exec-error] " + str(err)[:200])

TURNSTILE_TOKEN_JS = """
try { if (window.turnstile && window.turnstile.getResponse) {
        var t = window.turnstile.getResponse(); if (t) return t; } } catch (e) {}
var el = document.querySelector('input[name="cf-turnstile-response"]');
return el ? (el.value || '') : '';
"""

TURNSTILE_EXISTS_JS = """
return document.querySelector(
    'input[name="cf-turnstile-response"], iframe[src*="challenges.cloudflare.com"], .cf-turnstile'
) !== null;
"""

TS_COORDS_JS = """
function findWidget(root) {
    var els = root.querySelectorAll(
        'iframe[src*="challenges.cloudflare.com"], .cf-turnstile, [id*="cf-chl"]');
    for (var i = 0; i < els.length; i++) {
        var r = els[i].getBoundingClientRect();
        if (r.width > 50 && r.height > 20) return els[i];
    }
    var all = root.querySelectorAll('*');
    for (var j = 0; j < all.length; j++) {
        if (all[j].shadowRoot) {
            var f = findWidget(all[j].shadowRoot);
            if (f) return f;
        }
    }
    return null;
}
function boxFromTokenInput() {
    var el = document.querySelector('input[name="cf-turnstile-response"]');
    var cur = el ? el.parentElement : null;
    for (var k = 0; k < 6 && cur; k++) {
        var r = cur.getBoundingClientRect();
        if (r.width > 100 && r.width < 600 && r.height > 30 && r.height < 140) return cur;
        cur = cur.parentElement;
    }
    return null;
}
var hit = findWidget(document) || boxFromTokenInput();
if (!hit) return null;
var hr = hit.getBoundingClientRect();
return {cx: Math.round(hr.x + 30), cy: Math.round(hr.y + hr.height / 2)};
"""

def wait_turnstile_present(sb, timeout=12):
    """Turnstile 脚本异步渲染, 轮询等待组件出现"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if js(sb, TURNSTILE_EXISTS_JS, False):
            return True
        time.sleep(1)
    return False

def _activate_window():
    if os.name == "nt":
        return
    for cls in ["chrome", "chromium", "Chromium", "Chrome", "google-chrome"]:
        try:
            r = subprocess.run(["xdotool", "search", "--onlyvisible", "--class", cls],
                               capture_output=True, text=True, timeout=3)
            wids = [w for w in r.stdout.strip().split("\n") if w.strip()]
            if wids:
                subprocess.run(["xdotool", "windowactivate", "--sync", wids[0]],
                               timeout=3, stderr=subprocess.DEVNULL)
                time.sleep(0.2)
                return
        except Exception:
            pass
    try:
        subprocess.run(["xdotool", "getactivewindow", "windowactivate"],
                       timeout=3, stderr=subprocess.DEVNULL)
    except Exception:
        pass

def _windows_mouse_click(x: int, y: int):
    try:
        user32 = ctypes.windll.user32
        user32.SetCursorPos(int(x), int(y))
        time.sleep(0.08)
        user32.mouse_event(0x0002, 0, 0, 0, 0)
        time.sleep(0.03)
        user32.mouse_event(0x0004, 0, 0, 0, 0)
    except Exception:
        pass

def _xdotool_click(x: int, y: int):
    if os.name == "nt":
        _windows_mouse_click(x, y)
        return
    _activate_window()
    try:
        subprocess.run(["xdotool", "mousemove", "--sync", str(x), str(y)],
                       timeout=3, stderr=subprocess.DEVNULL)
        time.sleep(0.15)
        subprocess.run(["xdotool", "click", "1"], timeout=2, stderr=subprocess.DEVNULL)
    except Exception:
        os.system(f"xdotool mousemove {x} {y} click 1 2>/dev/null")

def turnstile_token(sb) -> str:
    token = js(sb, TURNSTILE_TOKEN_JS, "")
    return token if isinstance(token, str) else ""

def _click_turnstile(sb, locate_timeout=10) -> bool:
    coords = None
    errors = []
    deadline = time.time() + locate_timeout
    while time.time() < deadline:
        coords = js(sb, TS_COORDS_JS, None, errors)
        if coords:
            break
        time.sleep(1)
    if not coords:
        print("⚠️ 无法定位 Turnstile 坐标, 打印现场诊断:")
        diag_turnstile_dom(sb, errors)
        return False
    try:
        wi = js(sb, "return '' + (window.screenX || 0) + '|' + (window.screenY || 0) + '|' +"
                    " window.outerHeight + '|' + window.innerHeight;", "")
        parts = str(wi).split("|")
        sx, sy = int(parts[0] or 0), int(parts[1] or 0)
        bar = int(parts[2] or 800) - int(parts[3] or 768)
        ax = int(coords["cx"]) + sx
        ay = int(coords["cy"]) + sy + bar
        print(f"🖱️ 底层点击 Turnstile ({ax}, {ay})")
        _xdotool_click(ax, ay)
        time.sleep(3)
        return True
    except Exception as e:
        print(f"⚠️ 底层点击失败: {e}")
        return False

def click_turnstile(sb) -> bool:
    if _click_turnstile(sb):
        return True
    coords = js(sb, TS_COORDS_JS)
    if coords:
        try:
            if hasattr(sb, "cdp") and hasattr(sb.cdp, "gui_click_x_y"):
                print(f"🖱️ 坐标点击 Turnstile ({coords['cx']},{coords['cy']})")
                sb.cdp.gui_click_x_y(int(coords["cx"]), int(coords["cy"]))
                return True
        except Exception as e:
            print(f"⚠️ 坐标点击失败: {e}")
    try:
        if hasattr(sb, "cdp") and hasattr(sb.cdp, "click_captcha"):
            print("🖱️ 兜底调用 cdp 点击 ...")
            sb.cdp.click_captcha()
            return True
        print("⚠️ 当前 SeleniumBase 版本无 sb.cdp.click_captcha")
    except Exception as e:
        print(f"⚠️ click_captcha 出错: {e}")
    return False

def solve_turnstile(sb, timeout=120, tag="Turnstile", first_wait=10) -> bool:
    """轮询 Turnstile token, 没有则周期性点击 (用户要求多等一会: 超时 120s, 首等 10s)"""
    if turnstile_token(sb):
        return True
    deadline = time.time() + timeout
    next_click = time.time() + first_wait
    clicks = 0
    while time.time() < deadline:
        if turnstile_token(sb):
            print(f"✅ {tag}已通过(已获取 token)")
            return True
        if time.time() >= next_click:
            clicks += 1
            print(f"🖱️ 第 {clicks} 次点击 Turnstile 验证框...")
            click_turnstile(sb)
            next_click = time.time() + 10 + random.uniform(0, 3)
        time.sleep(2)
    ok = bool(turnstile_token(sb))
    print(f"{'✅' if ok else '❌'} {tag}{'已通过' if ok else '未通过'}")
    return ok

def handle_turnstile(sb) -> bool:
    print("🛡️ 处理 Turnstile 验证...")
    return solve_turnstile(sb, timeout=120, tag="Turnstile", first_wait=10)

# ================= 面板操作工具 =================
DUMP_BUTTONS_JS = """
var els = document.querySelectorAll('button, a, input[type="submit"], input[type="button"], [role="button"]');
var out = [];
for (var i = 0; i < els.length; i++) {
    var el = els[i];
    var t = (el.innerText || el.value || el.getAttribute('aria-label') || '').trim().replace(/\\s+/g, ' ');
    if (!t) continue;
    var r = el.getBoundingClientRect();
    var inDlg = false;
    try { inDlg = !!el.closest('[role="dialog"], .modal, [class*="modal"], [class*="dialog"]'); } catch (e) {}
    out.push({i: i, tag: el.tagName.toLowerCase(), text: t.slice(0, 80),
              disabled: !!el.disabled, visible: (r.width > 0 && r.height > 0), in_dialog: inDlg});
}
return out;
"""

def page_text(sb) -> str:
    """取页面可见文本 (JS 直取, 比 sb.get_page_text 快)"""
    t = js(sb, "return document.body ? document.body.innerText : '';", "")
    if isinstance(t, str) and t.strip():
        return t
    try:
        return sb.get_page_text()
    except Exception:
        return ""

def dump_buttons(sb, label="页面按钮"):
    """打印页面上所有带文案的按钮/链接 (诊断用, 首跑核对 DOM 的关键日志)"""
    btns = js(sb, DUMP_BUTTONS_JS, []) or []
    print(f"🔘 {label} (共 {len(btns)} 个带文案):")
    for b in btns[:40]:
        try:
            print(f"   [{'v' if b.get('visible') else 'h'}]"
                  f"{'[D]' if b.get('disabled') else '   '}"
                  f"{'[dlg]' if b.get('in_dialog') else '     '}"
                  f" <{b.get('tag')}> {b.get('text')}")
        except Exception:
            pass
    return btns

def find_button(btns, keywords, visible_only=True, enabled_only=True, dialog_first=True):
    """按关键词优先级在按钮列表里找候选, 返回 (button_dict, keyword) 或 (None, None)"""
    cands = []
    for kw in keywords:
        for b in btns:
            if kw in (b.get("text") or ""):
                if visible_only and not b.get("visible"):
                    continue
                if enabled_only and b.get("disabled"):
                    continue
                cands.append((kw, b))
        if cands:
            break
    if not cands:
        return None, None
    if dialog_first:
        for kw, b in cands:
            if b.get("in_dialog"):
                return b, kw
    return cands[0][1], cands[0][0]

def click_button_by_index(sb, idx: int, label: str) -> bool:
    """按 dump 出的序号点按钮: 先打标记再用 SB 点, 失败则 JS 直点"""
    marked = js(sb, f"""
        var els = document.querySelectorAll('button, a, input[type="submit"], input[type="button"], [role="button"]');
        var el = els[{int(idx)}];
        if (!el) return false;
        el.setAttribute('data-mws-target', '1');
        return true;
    """, False)
    if not marked:
        print(f"⚠️ 标记按钮 #{idx} 失败 ({label})")
        return False
    try:
        sb.click('[data-mws-target="1"]', timeout=10)
        print(f"✅ 已点击按钮 ({label})")
        return True
    except Exception as e:
        print(f"⚠️ SB 点击失败, 改用 JS 直点 ({label}): {type(e).__name__}")
        try:
            js(sb, f"""
                var el = document.querySelector('[data-mws-target="1"]');
                if (el) {{ el.click(); return true; }} return false;
            """, False)
            sb.sleep(2)
            return True
        except Exception as e2:
            print(f"❌ JS 点击也失败: {e2}")
            return False

def extract_remaining(text: str):
    """从页面文本提取剩余时间, 返回 (展示文案, 小时数或 None)"""
    if not text:
        return None, None
    patterns = [
        (r"残り\s*(\d+)\s*日", lambda m: (f"残り{m.group(1)}日", int(m.group(1)) * 24)),
        (r"残り\s*(\d+)\s*時間", lambda m: (f"残り{m.group(1)}時間", int(m.group(1)))),
        (r"剩余\s*(\d+)\s*天", lambda m: (f"剩余{m.group(1)}天", int(m.group(1)) * 24)),
        (r"剩余\s*(\d+)\s*(?:小时|小時)", lambda m: (f"剩余{m.group(1)}小时", int(m.group(1)))),
        (r"(\d+)\s*days?\s*(?:left|remaining)", lambda m: (f"{m.group(1)} days left", int(m.group(1)) * 24)),
        (r"(\d+)\s*hours?\s*(?:left|remaining)", lambda m: (f"{m.group(1)} hours left", int(m.group(1)))),
        (r"(\d{1,3}):(\d{2}):(\d{2})", lambda m: (f"倒计时 {m.group(0)}", int(m.group(1)) + int(m.group(2)) / 60)),
    ]
    for pat, fmt in patterns:
        m = re.search(pat, text)
        if m:
            try:
                return fmt(m)
            except Exception:
                continue
    return None, None

def page_is_login(sb) -> bool:
    """判断当前页是否登录页 (COOKIE 失效被踢回登录)"""
    try:
        url = sb.get_current_url()
    except Exception:
        url = ""
    if "/login" in url or "/auth" in url:
        return True
    try:
        if sb.is_element_present('input[type="password"]'):
            return True
    except Exception:
        pass
    txt = page_text(sb)
    for marker in ["Discord でログイン", "Login with Discord", "ログインして続行"]:
        if marker in txt:
            return True
    return False

def inject_cookie_and_open_dashboard(sb) -> bool:
    """注入 COOKIE 并打开面板, 返回是否已登录"""
    print(f"🌐 打开 {SITE_ORIGIN}/ (先拿域名上下文再注 cookie)")
    sb.open(SITE_ORIGIN + "/")
    sb.wait_for_ready_state_complete()
    sb.sleep(5)
    print(f"📝 当前 URL: {sb.get_current_url()} | Title: {sb.get_title()}")
    print(f"🍪 注入 COOKIE {COOKIE_NAME}={mask_secret(COOKIE)}")
    try:
        # __Host- 前缀 cookie 不得带 Domain 属性, 省略 domain 让浏览器默认当前域
        sb.driver.add_cookie({"name": COOKIE_NAME, "value": COOKIE,
                              "path": "/", "secure": True})
    except Exception as e:
        print(f"❌ 注入 COOKIE 失败: {e}")
        return False
    print("🔄 跳转面板页使 COOKIE 生效 (根路径是登录页, 刷新它不会进面板)...")
    sb.open(SITE_ORIGIN + "/dashboard")
    sb.wait_for_ready_state_complete()
    sb.sleep(6)
    print(f"📝 面板页 URL: {sb.get_current_url()} | Title: {sb.get_title()}")
    if page_is_login(sb):
        print("❌ 被踢回登录页, COOKIE 可能已失效")
        return False
    print("✅ 已登录面板")
    return True

def locate_server_page(sb, sid: str) -> bool:
    """定位服务器页面: 先看面板首页有没有该 id, 再试候选详情 URL"""
    txt = page_text(sb)
    if sid in txt:
        print(f"✅ 面板首页已列出服务器 {sid}")
        return True
    # 找 href 里带 id 的链接 (服务器卡片/详情入口)
    href = js(sb, f"""
        var as = document.querySelectorAll('a[href*="{sid}"]');
        if (!as.length) return '';
        return as[0].getAttribute('href') || '';
    """, "")
    if href:
        url = href if href.startswith("http") else SITE_ORIGIN + href
        print(f"🔗 找到服务器链接: {url}")
        sb.open(url)
        sb.wait_for_ready_state_complete()
        sb.sleep(5)
        if sid in page_text(sb):
            print(f"✅ 已打开服务器 {sid} 详情页")
            return True
    candidates = [
        f"{SITE_ORIGIN}/bots/{sid}",
        f"{SITE_ORIGIN}/servers/{sid}",
        f"{SITE_ORIGIN}/dashboard",
        f"{SITE_ORIGIN}/panel",
        f"{SITE_ORIGIN}/console",
    ]
    for url in candidates:
        print(f"🔍 尝试候选 URL: {url}")
        try:
            sb.open(url)
            sb.wait_for_ready_state_complete()
            sb.sleep(5)
        except Exception as e:
            print(f"⚠️ 打开失败: {e}")
            continue
        print(f"   Title: {sb.get_title()} | URL: {sb.get_current_url()}")
        t = page_text(sb)
        if sid in t:
            print(f"✅ 在 {url} 找到服务器 {sid}")
            return True
        if any(k in t for k in RENEW_KEYWORDS):
            print(f"⚠️ {url} 未见服务器 id, 但有续期相关文案, 继续用此页尝试")
            return True
    print(f"❌ 所有候选页都没找到服务器 {sid}")
    return False

def renew_one_server(sb, sid: str):
    """续期一台服务器, 返回 (status, remaining_hours, detail)
       status: success / not_needed / failed"""
    print(f"\n{'=' * 20} 服务器 [{sid}] {'=' * 20}")
    if not locate_server_page(sb, sid):
        txt = page_text(sb)
        print("📄 当前页文本片段(前 500 字):")
        print("   " + txt[:500].replace("\n", " | "))
        return "failed", None, "未找到服务器页面 (面板结构可能变了, 请看日志核对)"

    before_text = page_text(sb)
    before_disp, before_hours = extract_remaining(before_text)
    if before_disp:
        print(f"📅 续期前剩余时间: {before_disp}")
    else:
        print("ℹ️ 续期前未识别到剩余时间文案")

    btns = dump_buttons(sb, f"服务器 {sid} 页面按钮")
    btn, kw = find_button(btns, RENEW_KEYWORDS)
    if btn is None:
        # 按钮不可用也算一种状态: 找找有没有被禁用的续期按钮
        disabled_btn, dkw = None, None
        for k in RENEW_KEYWORDS:
            for b in btns:
                if k in (b.get("text") or "") and b.get("disabled"):
                    disabled_btn, dkw = b, k
                    break
            if disabled_btn:
                break
        if disabled_btn:
            print(f"ℹ️ 续期按钮存在但不可用: '{disabled_btn.get('text')}' (可能未到续期时间/冷却中)")
            return "not_needed", before_hours, f"续期按钮不可用: {disabled_btn.get('text')}"
        print("ℹ️ 未找到续期按钮 (可能已续期, 或页面结构变化)")
        return "not_needed", before_hours, "未找到续期按钮, 疑似已续期 (请核对按钮列表日志)"

    print(f"✅ 找到续期按钮: '{btn.get('text')}' (关键词 {kw}, tag={btn.get('tag')})")
    if not click_button_by_index(sb, btn["i"], f"续期按钮 '{btn.get('text')}'"):
        return "failed", before_hours, "点击续期按钮失败"

    # 点完等一会: 可能弹出 Turnstile 或确认框
    sb.sleep(5)
    if wait_turnstile_present(sb, timeout=15):
        print("🛡️ 续期后出现 Turnstile")
        if not handle_turnstile(sb):
            print("❌ 续期 Turnstile 未通过, 继续观察页面状态 (可能仍需手动处理)")
    else:
        print("ℹ️ 续期后未检测到 Turnstile")

    # 确认弹窗 (有的面板要点两次确认)
    sb.sleep(3)
    btns2 = dump_buttons(sb, "续期点击后按钮")
    confirm_btn, ckw = find_button(btns2, CONFIRM_KEYWORDS)
    if confirm_btn:
        print(f"✅ 找到确认按钮: '{confirm_btn.get('text')}' (关键词 {ckw})")
        click_button_by_index(sb, confirm_btn["i"], f"确认按钮 '{confirm_btn.get('text')}'")
        sb.sleep(5)
        if wait_turnstile_present(sb, timeout=15):
            print("🛡️ 确认后出现 Turnstile")
            handle_turnstile(sb)
    else:
        print("ℹ️ 未见确认弹窗按钮")

    # 等服务端处理, 再抓结果
    print("⏳ 等待服务端处理 (15s)...")
    sb.sleep(15)
    after_text = page_text(sb)
    print("📄 续期后页面文本片段(前 500 字):")
    print("   " + after_text[:500].replace("\n", " | "))
    after_disp, after_hours = extract_remaining(after_text)
    if after_disp:
        print(f"📅 续期后剩余时间: {after_disp}")

    strong_hit = any(k in after_text for k in
                     ["延長しました", "续期成功", "延長成功", "更新しました",
                      "完了しました", "extended", "renewed"])
    generic_hit = "成功" in after_text
    increased = (before_hours is not None and after_hours is not None
                 and after_hours > before_hours + 1)
    if strong_hit or increased:
        detail = after_disp or "剩余时间已更新"
        print(f"✅ 续期成功 ({detail})")
        return "success", after_hours, detail
    if generic_hit:
        print("✅ 续期疑似成功 (仅命中通用'成功'文案)")
        return "success", after_hours, (after_disp or "页面出现'成功'")

    # 没命中成功文案: 刷新再看一次 (有的站是 AJAX 更新, 刷新才显示新时间)
    print("🔄 未见明确成功文案, 刷新页面再确认一次...")
    sb.refresh()
    sb.wait_for_ready_state_complete()
    sb.sleep(6)
    after2 = page_text(sb)
    after_disp2, after_hours2 = extract_remaining(after2)
    if after_disp2:
        print(f"📅 刷新后剩余时间: {after_disp2}")
    strong_hit2 = any(k in after2 for k in
                      ["延長しました", "续期成功", "延長成功", "更新しました",
                       "完了しました", "extended", "renewed"])
    increased2 = (before_hours is not None and after_hours2 is not None
                  and after_hours2 > before_hours + 1)
    if strong_hit2 or increased2:
        return "success", after_hours2, (after_disp2 or "刷新后确认剩余时间已更新")
    # 按钮变冷却态也算成功
    btns3 = dump_buttons(sb, "刷新后按钮")
    for b in btns3:
        t = b.get("text") or ""
        if re.search(r"(残り|剩余|Renew in|cooldown)", t):
            print(f"✅ 刷新后按钮变为冷却态: '{t}'")
            return "success", after_hours2, f"按钮冷却态: {t[:60]}"
    print("❌ 未能确认续期成功")
    return "failed", after_hours2, "点击续期后未见成功文案且剩余时间未变 (请看日志核对)"

# ================= 主流程 =================
def main():
    print("#" * 25)
    print("   MWS 自动续期 (浏览器版)")
    print("#" * 25)

    server_list = [s.strip() for s in SERVER_IDS.split(",") if s.strip()]
    if not server_list:
        print("❌ 未配置有效的 SERVER_IDS")
        send_tg_notification(format_notification(
            "❌ 续期失败", [], extra="⚠️ 未配置 SERVER_IDS"))
        return
    if not COOKIE:
        print("❌ 未配置 COOKIE, 请在 Secrets 里填 __Host-mrtcloud_token")
        send_tg_notification(format_notification(
            "❌ 续期失败", [], extra="⚠️ 未配置 COOKIE"))
        return

    expiry = get_expiry_from_cookie(COOKIE)
    if expiry:
        print(f"📅 当前 COOKIE 到期时间: {expiry.strftime('%Y-%m-%d %H:%M:%S')}")
    else:
        print("⚠️ 无法从 COOKIE 解析到期时间")

    sb_kwargs = {"uc": True, "headless": HEADLESS, "locale_code": "ja-JP"}
    if IS_PROXY:
        print(f"🔗 挂载代理: {PROXY_SERVER}")
        sb_kwargs["proxy"] = PROXY_SERVER
    else:
        print("🍭 未使用代理, 直连访问")

    results = {}  # sid -> (status, hours, detail)
    with SB(**sb_kwargs) as sb:
        try:
            sb.set_window_size(1440, 900)
        except Exception:
            pass
        # 出口 IP 自检 (走代理时确认出口真是节点 IP)
        try:
            proxies = {"http": PROXY_SERVER, "https": PROXY_SERVER} if IS_PROXY else None
            ip = requests.get("https://api.ip.sb/ip", proxies=proxies, timeout=15).text.strip()
            print(f"📍 当前出口 IP: {ip}")
        except Exception as e:
            print(f"⚠️ 获取出口 IP 失败: {e}")

        print("🚀 启动浏览器...")
        try:
            logged_in = inject_cookie_and_open_dashboard(sb)
        except Exception as e:
            print(f"❌ 打开面板异常: {type(e).__name__}: {e}")
            logged_in = False

        if not logged_in:
            lines = [f"🖥 服务器: [{s}] 登录失败 (COOKIE 可能已失效, 请手动更新 Secret)"
                     for s in server_list]
            send_tg_notification(format_notification("❌ 登录失败", lines))
            print("🏁 脚本执行完毕")
            return

        # 面板首页如有 Turnstile 先处理掉
        try:
            if wait_turnstile_present(sb, timeout=15):
                print("🛡️ 面板首页出现 Turnstile")
                handle_turnstile(sb)
            else:
                print("ℹ️ 面板首页未检测到 Turnstile")
        except Exception as e:
            print(f"⚠️ 首页 Turnstile 处理异常: {e}")

        for sid in server_list:
            try:
                results[sid] = renew_one_server(sb, sid)
            except Exception as e:
                import traceback
                traceback.print_exc()
                results[sid] = ("failed", None, f"脚本异常: {type(e).__name__}: {str(e)[:150]}")
            time.sleep(3)

    # ---------- 汇总 TG 通知 (沿用旧版格式) ----------
    server_lines = []
    all_ok = True
    for sid in server_list:
        status, hours, detail = results.get(sid, ("failed", None, "未知"))
        if status == "success":
            h = f"剩余 {hours:.1f} H" if hours is not None else (detail or "已续期")
            server_lines.append(f"🖥 服务器: [{sid}] 续期成功, {h}")
        elif status == "not_needed":
            # 无需续期不算失败, all_ok 保持不变
            h = f"剩余 {hours:.1f} H" if hours is not None else ""
            server_lines.append(f"🖥 服务器: [{sid}] 无需续期 {h} ({detail})")
        else:
            all_ok = False
            server_lines.append(f"🖥 服务器: [{sid}] 续期失败 ({detail})")
    status_line = "✅ 全部续期成功" if all_ok else "⚠️ 部分服务器续期失败, 请手动检查"
    send_tg_notification(format_notification(status_line, server_lines))
    print("🏁 脚本执行完毕")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        traceback.print_exc()
        try:
            send_tg_notification(format_notification(
                "❌ 脚本异常退出", [], extra=f"⚠️ {type(e).__name__}: {str(e)[:200]}"))
        except Exception:
            pass
