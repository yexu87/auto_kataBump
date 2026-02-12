import os
import platform
import time
from datetime import datetime, timedelta, timezone
import re
import traceback
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
    只脱敏 @ 前面的用户名
    """
    e = (email or "").strip()
    if "@" not in e:
        return "***"

    name, domain = e.split("@", 1)
    if len(name) <= 1:
        name_mask = name or "*"
    elif len(name) == 2:
        name_mask = name[0] + name[1]
    else:
        name_mask = name[0] + ("*" * (len(name) - 2)) + name[-1]

    return f"{name_mask}@{domain}"


def setup_xvfb():
    """在 Linux 上启动 Xvfb（无 DISPLAY 时）"""
    if platform.system().lower() == "linux" and not os.environ.get("DISPLAY"):
        try:
            display = Display(visible=False, size=(1920, 1080))
            display.start()
            os.environ["DISPLAY"] = display.new_display_var
            print("🖥️ Xvfb 已启动")
            return display
        except Exception as e:
            print(f"⚠️ 启动 Xvfb 失败 (非致命): {e}")
    return None


def screenshot(sb, name: str):
    """保存截图"""
    try:
        path = f"{SCREENSHOT_DIR}/{name}"
        sb.save_screenshot(path)
        print(f"📸 截图已保存: {path}")
    except Exception as e:
        print(f"⚠️ 截图失败: {e}")


def tg_send(text: str, token: Optional[str] = None, chat_id: Optional[str] = None):
    """发送 Telegram 消息"""
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
        print(f"⚠️ TG 发送失败：{e}")


def get_expiry(sb) -> Optional[str]:
    """
    安全获取服务器 Expiry 字符串
    """
    try:
        # 先检查是否存在
        if sb.is_element_visible("//div[contains(text(),'Expiry')]"):
            text = sb.get_text("//div[contains(text(),'Expiry')]/following-sibling::div")
            return text.strip() if text else None
    except Exception:
        pass
    return None


def renew_open_utc_from_expiry(expiry_str: str) -> datetime:
    try:
        d = datetime.strptime(expiry_str.strip(), "%Y-%m-%d").date()
        return datetime(d.year, d.month, d.day, tzinfo=timezone.utc) - timedelta(days=1)
    except ValueError:
        # 如果格式不对，返回一个默认时间
        return datetime.now(timezone.utc)


def should_renew_utc0(expiry_str: str, now_utc: datetime = None) -> bool:
    """
    以 UTC 0 点作为对比基准
    """
    if not expiry_str:
        return False
        
    try:
        expiry_date = datetime.strptime(expiry_str.strip(), "%Y-%m-%d").date()
    except ValueError:
        print(f"⚠️ 日期格式解析错误: {expiry_str}")
        return False

    renew_open_utc = datetime(expiry_date.year, expiry_date.month, expiry_date.day, tzinfo=timezone.utc) - timedelta(days=1)
    now_utc = now_utc or datetime.now(timezone.utc)

    print(f"🕒 now_utc        = {now_utc.strftime('%Y-%m-%d %H:%M')} UTC")
    print(f"🕒 renew_open_utc = {renew_open_utc.strftime('%Y-%m-%d %H:%M')} UTC")

    if now_utc >= renew_open_utc:
        return True

    delta = renew_open_utc - now_utc
    mins = int(delta.total_seconds() // 60)
    print(f"⏳ 距离可续期还差: {mins//60} 小时 {mins%60} 分钟（按 UTC0 点）")
    return False


def build_accounts_from_env() -> List[Dict[str, str]]:
    batch = (os.getenv("KATABUMP_BATCH") or "").strip()
    if not batch:
        raise RuntimeError("❌ 缺少环境变量：请设置 KATABUMP_BATCH")

    accounts: List[Dict[str, str]] = []
    for idx, raw in enumerate(batch.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        parts = [p.strip() for p in line.split(",")]

        if len(parts) not in (3, 5):
            print(f"⚠️ 跳过格式错误的行 ({idx}): {raw}")
            continue

        email, password, server_id = parts[0], parts[1], parts[2]
        tg_token = parts[3] if len(parts) == 5 else ""
        tg_chat = parts[4] if len(parts) == 5 else ""

        if not email or not password or not server_id:
            print(f"⚠️ 跳过空字段行 ({idx}): {raw}")
            continue

        accounts.append({
            "email": email,
            "password": password,
            "server_id": server_id,
            "tg_token": tg_token,
            "tg_chat": tg_chat,
        })

    if not accounts:
        raise RuntimeError("❌ KATABUMP_BATCH 里没有有效账号行")

    return accounts


def renew_one_account(email: str, password: str, server_id: str) -> Tuple[str, Optional[str], Optional[str]]:
    """
    续期单个账号
    返回：(status, expiry_before, expiry_after_or_msg)
    """
    renew_url = RENEW_URL_TEMPLATE.format(server_id=server_id)
    expiry_before = None

    try:
        # 使用 uc=True 模式启动浏览器
        with SB(uc=True, locale="en", test=True) as sb:
            print("🚀 浏览器启动（UC Mode）")

            # ===== 1. 登录流程 =====
            print(f"👉 正在登录: {email} ...")
            try:
                sb.uc_open_with_reconnect(LOGIN_URL, reconnect_time=5.0)
                time.sleep(3)
                
                # 检查是否还在登录页
                if sb.is_element_visible('input[name="email"]'):
                    sb.type('input[name="email"]', email)
                    sb.type('input[name="password"]', password)
                    
                    # 尝试处理 Cloudflare 点击
                    if sb.is_element_visible("iframe[src*='challenges']"):
                         print("🧩 检测到 CF 验证码，尝试点击...")
                         sb.uc_gui_click_captcha()
                         time.sleep(2)

                    sb.click('button[type="submit"]')
                    sb.wait_for_element_visible("body", timeout=30)
                    time.sleep(3)
            except Exception as e:
                print(f"⚠️ 登录过程出现异常: {e}")
                # 不立即返回，尝试继续，也许已经登录了

            # ===== 2. 检查登录状态 =====
            if sb.is_element_visible('input[name="email"]'):
                print("❌ 登录失败：页面依然在登录框。")
                screenshot(sb, f"login_fail_{server_id}.png")
                return "FAIL", None, "Login Failed (Page Stuck)"

            # ===== 3. 进入服务器详情页 =====
            print(f"👉 跳转到服务器页: {server_id} ...")
            sb.uc_open_with_reconnect(renew_url, reconnect_time=5.0)
            sb.wait_for_element_visible("body", timeout=30)
            time.sleep(3)

            # 检查 404
            if "404" in sb.get_page_title() or "not found" in (sb.get_text("body") or "").lower():
                 print("❌ 页面 404：可能是 Server ID 错误。")
                 return "FAIL", None, "Page 404"

            # ===== 4. 获取当前 Expiry =====
            expiry_before = get_expiry(sb)
            
            if not expiry_before:
                print("❌ 未找到 Expiry 元素，可能登录失效或布局变更。")
                screenshot(sb, f"no_expiry_{server_id}.png")
                return "FAIL", None, "Expiry Element Not Found"

            print(f"📅 当前 Expiry: {expiry_before}")

            # 检查是否需要续期
            if not should_renew_utc0(expiry_before):
                print("ℹ️ 还没到续期时间（按 UTC0 点规则）")
                return "SKIP", expiry_before, None

            print("🔔 到续期时间，开始续期流程...")

            # ===== 5. 点击 Renew 按钮 =====
            if not sb.is_element_visible("button:contains('Renew')"):
                print("❌ 找不到 Renew 按钮")
                screenshot(sb, f"no_renew_btn_{server_id}.png")
                return "FAIL", expiry_before, "No Renew Btn"

            sb.click("button:contains('Renew')")
            sb.wait_for_element_visible("#renew-modal", timeout=20)
            time.sleep(2)

            # ===== 6. 处理 Renew Modal 中的 Turnstile =====
            print("🧩 检查 Modal 验证码...")
            try:
                # 尝试点击任何可能的验证码 iframe
                if sb.is_element_visible("iframe[src*='challenges']"):
                    sb.uc_gui_click_captcha()
                    time.sleep(4)
            except Exception as e:
                print(f"⚠️ captcha 点击异常: {e}")

            # ===== 7. 提交 Renew =====
            # 使用 JS 强制提交，通常比点击 submit 按钮更稳
            sb.execute_script("document.querySelector('#renew-modal form').submit();")
            print("📤 已提交续期请求...")
            
            # 等待结果（页面可能会刷新或弹出提示）
            time.sleep(5)

            # ===== 8. 检查结果/告警 =====
            NOT_YET_SEL = 'div.alert.alert-danger'
            if sb.is_element_visible(NOT_YET_SEL):
                alert_text_raw = (sb.get_text(NOT_YET_SEL) or "").strip()
                print(f"🔎 网站返回告警: [{alert_text_raw}]")
                screenshot(sb, f"renew_alert_{server_id}.png")

                # 清洗文本以匹配“未到期”提示
                clean_text = re.sub(r"\s+", " ", alert_text_raw).replace("×", "").strip()
                if "renew your server yet" in clean_text.lower():
                    return "OK_NOT_YET", expiry_before, alert_text_raw
                
                return "FAIL", expiry_before, alert_text_raw

            # ===== 9. 刷新检查 Expiry 是否更新 =====
            try:
                sb.refresh()
                sb.wait_for_element_visible("body", timeout=30)
                time.sleep(3)
                expiry_after = get_expiry(sb)
            except Exception:
                expiry_after = None

            if expiry_after and expiry_after != expiry_before:
                print(f"🎉 Expiry 已更新: {expiry_before} -> {expiry_after}")
                return "OK", expiry_before, expiry_after

            print("✅ 流程结束（Expiry 未立即变化，但也未报错）")
            return "OK", expiry_before, expiry_after

    except Exception as e:
        print(f"💥 发生严重异常: {e}")
        traceback.print_exc()
        # 这里的关键修复：返回一个由3个元素组成的元组，避免 main 函数解包失败
        return "FAIL", expiry_before, str(e)


def main():
    try:
        accounts = build_accounts_from_env()
    except Exception as e:
        print(e)
        return

    display = setup_xvfb()

    ok = fail = skip = 0
    not_yet = 0
    tg_dests = set()

    try:
        for i, acc in enumerate(accounts, start=1):
            email = acc["email"]
            password = acc["password"]
            server_id = acc["server_id"]
            tg_token = (acc.get("tg_token") or "").strip()
            tg_chat = (acc.get("tg_chat") or "").strip()
            
            if tg_token and tg_chat:
                tg_dests.add((tg_token, tg_chat))

            safe_email = mask_email_keep_domain(email)
            print("\n" + "=" * 70)
            print(f"👤 [{i}/{len(accounts)}] 账号： {safe_email} (ID: {server_id})")
            print("=" * 70)

            # 调用核心函数
            status, before, after = renew_one_account(email, password, server_id)

            # 处理结果
            if status == "SKIP":
                skip += 1
                现在_utc = datetime.now(timezone.utc)
                open_utc = renew_open_utc_from_expiry(before) if before else now_utc
                msg = (
                    "ℹ️ Katabump 续期跳过 (未到时间)\n"
                    f"账号：{safe_email}\n"
                    f"Expiry：{before}\n"
                    f"开放时间：{open_utc.strftime('%Y-%m-%d %H:%M')} UTC"
                )
            
            elif status == "OK":
                ok += 1
                if after and after != before:
                    msg = f"✅ Katabump 续期成功\n账号：{safe_email}\nExpiry：{before} ➜ {after}"
                else:
                    msg = f"✅ Katabump 已提交续期 (日期未立即刷新)\n账号：{safe_email}\nExpiry：{before}"
            
            elif status == "OK_NOT_YET":
                not_yet += 1
                msg = (
                    "ℹ️ Katabump 续期跳过 (网站提示未到期)\n"
                    f"账号：{safe_email}\n"
                    f"Expiry：{before}\n"
                    f"提示：{after}"
                )
            
            else: # FAIL
                fail += 1
                msg = f"❌ Katabump 续期失败\n账号：{safe_email}\n当前Expiry：{before or '未知'}\n错误信息：{after}"

            print(msg)
            tg_send(msg, tg_token, tg_chat)

            # 账号间休息，避免封控
            if i < len(accounts):
                print("⏳ 等待 5 秒切换下一个账号...")
                time.sleep(5)

        summary = f"📌 汇总：续期成功 {ok} / 网站提示未到期 {not_yet} / 脚本跳过 {skip} / 失败 {fail}"
        print("\n" + summary)
        
        for token, chat in sorted(tg_dests):
            tg_send(summary, token, chat)

    except KeyboardInterrupt:
        print("\n🚫 用户中断")
    finally:
        if display:
            display.stop()


if __name__ == "__main__":
    main()
