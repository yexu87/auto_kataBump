import os
import platform
import time
from datetime import datetime
from typing import List, Dict, Optional, Tuple

import requests
from seleniumbase import SB
from pyvirtualdisplay import Display

"""
必须每天运行一次
环境变量格式如下(英文逗号分割)：
email,password,server_id,tg_bot_token,tg_chat_id

每行一套数据：
1、不发 TG：email,password,server_id
2、发 TG：email,password,server_id,tg_bot_token,tg_chat_id

注意:server_id为续期界面中的url里面的id编号，每个人的id都会不一样

export KATABUMP_BATCH='a1@example.com,pass1,218445,123456:AAxxxxxx,123456789
a2@example.com,pass2,998877,123456:AAyyyyyy,-10022223333
a3@example.com,pass3,556677
'

"""

LOGIN_URL = "https://dashboard.katabump.com/login"
RENEW_URL_TEMPLATE = "https://dashboard.katabump.com/servers/edit?id={server_id}"

SCREENSHOT_DIR = "screenshots"
os.makedirs(SCREENSHOT_DIR, exist_ok=True)


def mask_email_keep_domain(email: str) -> str:
    """
    只脱敏 @ 前面的用户名：
    - 保留第 1 个和最后 1 个字符
    - 中间有几个字符就用几个 *（星号数量 = 中间字符数量）
    - @ 后面的域名原样保留
    例：abcdef@gmail.com -> a****f@gmail.com
    """
    e = (email or "").strip()
    if "@" not in e:
        return "***"

    name, domain = e.split("@", 1)
    if len(name) <= 1:
        name_mask = name or "*"
    elif len(name) == 2:
        # 中间字符数为0，所以不加 *
        name_mask = name[0] + name[1]
    else:
        name_mask = name[0] + ("*" * (len(name) - 2)) + name[-1]

    return f"{name_mask}@{domain}"


def setup_xvfb():
    """在 Linux 上启动 Xvfb（无 DISPLAY 时）"""
    if platform.system().lower() == "linux" and not os.environ.get("DISPLAY"):
        display = Display(visible=False, size=(1920, 1080))
        display.start()
        os.environ["DISPLAY"] = display.new_display_var
        print("🖥️ Xvfb 已启动")
        return display
    return None


def screenshot(sb, name: str):
    """保存截图"""
    path = f"{SCREENSHOT_DIR}/{name}"
    sb.save_screenshot(path)
    print(f"📸 {path}")


def tg_send(text: str, token: Optional[str] = None, chat_id: Optional[str] = None):
    """发送 Telegram 消息（每个账号独立 token/chat_id；不配置则跳过）"""
    token = (token or "").strip()
    chat_id = (chat_id or "").strip()
    if not token or not chat_id:
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        requests.post(
            url,
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            timeout=15,
        ).raise_for_status()
    except Exception as e:
        # TG 失败不影响主流程
        print(f"⚠️ TG 发送失败：{e}")


def get_expiry(sb) -> str:
    """获取服务器 Expiry 字符串（页面上通常是 YYYY-MM-DD）"""
    return sb.get_text("//div[contains(text(),'Expiry')]/following-sibling::div").strip()


def parse_expiry_date(expiry_str: str) -> datetime:
    """把 Expiry 字符串解析为 datetime（YYYY-MM-DD）"""
    return datetime.strptime(expiry_str, "%Y-%m-%d")


def should_renew(expiry_str: str) -> bool:
    """判断是否到续期时间（到期前一天）"""
    expiry_date = parse_expiry_date(expiry_str)
    today = datetime.today()
    return (expiry_date - today).days == 1


def build_accounts_from_env() -> List[Dict[str, str]]:
    """
    统一账号来源：只使用 KATABUMP_BATCH（多行，每行一个账号）。

    格式（仅支持逗号分隔）：
      1) email,password,server_id
      2) email,password,server_id,tg_bot_token,tg_chat_id   （可选：为该账号单独指定 TG）

    规则：
      - 行首/行尾空格会被忽略
      - 空行与 # 开头注释行会被忽略
      - 不写 TG 就不发 TG（不会回退任何全局 TG 变量）
    """
    batch = (os.getenv("KATABUMP_BATCH") or "").strip()
    if not batch:
        raise RuntimeError("❌ 缺少环境变量：请设置 KATABUMP_BATCH（即使只有一个账号也用它）")

    accounts: List[Dict[str, str]] = []
    for idx, raw in enumerate(batch.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        parts = [p.strip() for p in line.split(",")]

        if len(parts) not in (3, 5):
            raise RuntimeError(
                f"❌ KATABUMP_BATCH 第 {idx} 行格式不对（必须是 email,password,server_id 或 email,password,server_id,tg_bot_token,tg_chat_id）：{raw!r}"
            )

        email, password, server_id = parts[0], parts[1], parts[2]
        tg_token = parts[3] if len(parts) == 5 else ""
        tg_chat = parts[4] if len(parts) == 5 else ""

        if not email or not password or not server_id:
            raise RuntimeError(f"❌ KATABUMP_BATCH 第 {idx} 行存在空字段：{raw!r}")

        accounts.append({
            "email": email,
            "password": password,
            "server_id": server_id,
            "tg_token": tg_token,
            "tg_chat": tg_chat,
        })

    if not accounts:
        raise RuntimeError("❌ KATABUMP_BATCH 里没有有效账号行（空行/注释行不算）")

    return accounts


def renew_one_account(email: str, password: str, server_id: str) -> Tuple[str, Optional[str], Optional[str]]:
    """
    续期单个账号。

    返回：(status, expiry_before, expiry_after)
    status:
      - "SKIP"  还没到续期时间
      - "OK"    已提交续期且 Expiry 有变化（或提交后可见更新）
      - "FAIL"  续期流程中断/疑似失败
    """
    renew_url = RENEW_URL_TEMPLATE.format(server_id=server_id)

    with SB(uc=True, locale="en", test=True) as sb:
        print("🚀 浏览器启动（UC Mode）")

        # ===== 登录 =====
        sb.uc_open_with_reconnect(LOGIN_URL, reconnect_time=5.0)
        time.sleep(2)
        sb.type('input[name="email"]', email)
        sb.type('input[name="password"]', password)
        sb.click('button[type="submit"]')
        sb.wait_for_element_visible("body", timeout=30)
        time.sleep(2)

        # ===== 打开续期页（关键：server_id 从环境变量/批量配置来）=====
        sb.uc_open_with_reconnect(renew_url, reconnect_time=5.0)
        sb.wait_for_element_visible("body", timeout=30)
        time.sleep(2)
        # screenshot(sb, f"id_{server_id}_01_page_loaded.png")

        # ===== 获取 Expiry 并检查是否需要续期 =====
        expiry_before = get_expiry(sb)
        print(f"📅 当前 Expiry: {expiry_before}")

        if not should_renew(expiry_before):
            print("ℹ️ 还没到续期时间，今天不续期")
            return "SKIP", expiry_before, None

        print("🔔 到续期时间，开始续期流程...")

        # ===== 打开 Renew Modal =====
        sb.click("button:contains('Renew')")
        sb.wait_for_element_visible("#renew-modal", timeout=20)
        time.sleep(2)
        # screenshot(sb, f"id_{server_id}_02_modal_open.png")

        # ===== 尝试 Turnstile 交互 =====
        try:
            sb.uc_gui_click_captcha()
            time.sleep(4)
        except Exception as e:
            print(f"⚠️ captcha 点击异常: {e}")

        # screenshot(sb, f"id_{server_id}_03_after_captcha.png")

        # ===== 检查 cookies =====
        cookies = sb.get_cookies()
        cf_clearance = next((c["value"] for c in cookies if c.get("name") == "cf_clearance"), None)
        print("🧩 cf_clearance:", "OK" if cf_clearance else "NONE")

        if not cf_clearance:
            # screenshot(sb, f"id_{server_id}_04_no_cf_clearance.png")
            print("❌ 未获取 cf_clearance，续期可能失败")
            return "FAIL", expiry_before, None

        # ===== 提交 Renew =====
        sb.execute_script("document.querySelector('#renew-modal form').submit();")
        time.sleep(3)
        # screenshot(sb, f"id_{server_id}_05_after_submit.png")

        # ===== 尝试刷新并再次读取 Expiry（不保证立即变，但尽量验证一下）=====
        try:
            sb.refresh()
            sb.wait_for_element_visible("body", timeout=30)
            time.sleep(2)
            expiry_after = get_expiry(sb)
        except Exception:
            expiry_after = None

        if expiry_after and expiry_after != expiry_before:
            print(f"🎉 Expiry 已更新：{expiry_before} -> {expiry_after}")
            return "OK", expiry_before, expiry_after

        print("✅ 已提交续期（Expiry 可能稍后更新）")
        return "OK", expiry_before, expiry_after


def main():
    accounts = build_accounts_from_env()
    display = setup_xvfb()

    ok = fail = skip = 0
    tg_dests = set()  # (token, chat_id) 去重

    try:
        for i, acc in enumerate(accounts, start=1):
            email = acc["email"]
            password = acc["password"]
            server_id = acc["server_id"]
            tg_token = (acc.get("tg_token") or "").strip()
            tg_chat = (acc.get("tg_chat") or "").strip()
            if tg_token and tg_chat:
                tg_dests.add((tg_token, tg_chat))

            

            email = acc["email"]
            safe_email = mask_email_keep_domain(email)
            print("\n" + "=" * 70)
            print(f"👤 [{i}/{len(accounts)}] 账号： {safe_email}")
            print("=" * 70)

            try:
                status, before, after = renew_one_account(email, password, server_id)

                if status == "SKIP":
                    skip += 1
                    msg = f"ℹ️ Katabump 续期跳过（未到期前一天）\n账号：{safe_email}\nExpiry：{before}"
                elif status == "OK":
                    ok += 1
                    if after and after != before:
                        msg = f"✅ Katabump 续期成功\n账号：{safe_email}\nExpiry：{before} ➜ {after}"
                    else:
                        msg = f"✅ Katabump 已提交续期（Expiry 可能稍后更新）\n账号：{safe_email}\nExpiry：{before}"
                else:
                    fail += 1
                    msg = f"❌ Katabump 续期失败/疑似失败\n账号：{safe_email}\nExpiry：{before or '未知'}"

                print(msg)
                tg_send(msg, tg_token, tg_chat)

            except Exception as e:
                fail += 1
                msg = f"❌ Katabump 脚本异常\n账号：{safe_email}\n错误：{e}"
                print(msg)
                tg_send(msg, tg_token, tg_chat)

            # 切下一个账号前等待 5 秒
            time.sleep(5)

            # 每个账号之间等待 5 秒，避免触发风控/频繁登录
            if i < len(accounts):
                time.sleep(5)

        summary = f"📌 本次批量完成：成功 {ok} / 跳过 {skip} / 失败 {fail}"
        print("\n" + summary)
        if tg_dests:
            for token, chat in sorted(tg_dests):
                tg_send(summary, token, chat)

    finally:
        if display:
            display.stop()


if __name__ == "__main__":
    main()
