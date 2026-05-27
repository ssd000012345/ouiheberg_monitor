#!/usr/bin/env python3
"""
OuiHeberg / OuiPanel 服务器自动启动脚本
功能：登录 OuiHeberg OAuth → 检测服务器状态 → 离线则点 Start
触发：Uptime Kuma 检测到离线时通过 webhook 触发 GitHub Actions
"""

import os
import sys
import time
import json
import shutil
import threading
import subprocess
import traceback
from pathlib import Path
from urllib.request import Request, urlopen
from seleniumbase import Driver

# ── 环境变量 ──────────────────────────────────────────────
EMAIL           = os.environ.get("OUIHEBERG_EMAIL", "").strip()
PASSWORD        = os.environ.get("OUIHEBERG_PASSWORD", "").strip()
SERVER_ID       = os.environ.get("OUIHEBERG_SERVER_ID", "").strip()
TG_BOT_TOKEN    = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID      = os.environ.get("TG_CHAT_ID", "").strip()
WX_APP_TOKEN    = os.environ.get("WX_APP_TOKEN", "").strip()
WX_UID          = os.environ.get("WX_UID", "").strip()
ENABLE_RECORDING = os.environ.get("ENABLE_RECORDING", "false").strip().lower() == "true"

# Uptime Kuma 传入的心跳状态（status=0 离线，1 在线）
_heartbeat_raw     = os.environ.get("UPTIME_HEARTBEAT", "").strip()
_uptime_status_raw = os.environ.get("UPTIME_STATUS", "").strip().lower()
try:
    _hb = json.loads(_heartbeat_raw) if _heartbeat_raw else {}
    UPTIME_STATUS = str(_hb.get("status", _uptime_status_raw)).lower()
    if UPTIME_STATUS == "1":
        UPTIME_STATUS = "up"
    elif UPTIME_STATUS == "0":
        UPTIME_STATUS = "down"
except Exception:
    UPTIME_STATUS = _uptime_status_raw

BASE_URL = "https://dash.ouipanel.com"

SCREENSHOT_DIR = Path("screenshots")
SCREENSHOT_DIR.mkdir(exist_ok=True)
REC_FRAME_DIR  = Path("screenshots/rec")
REC_FRAME_DIR.mkdir(exist_ok=True)
RECORDING_DIR  = Path("recordings")
RECORDING_DIR.mkdir(exist_ok=True)

# ── 日志 ──────────────────────────────────────────────────
def log(msg):  print(f"[INFO]  {msg}", flush=True)
def warn(msg): print(f"[WARN]  {msg}", flush=True)
def err(msg):  print(f"[ERROR] {msg}", flush=True)

# ── 推送通知 ──────────────────────────────────────────────
def send_tg(text: str):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return
    try:
        req = Request(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            data=json.dumps({"chat_id": TG_CHAT_ID, "text": text}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req, timeout=15):
            log("TG 推送成功")
    except Exception as e:
        warn(f"TG 推送失败: {e}")

def send_wx(title: str, content: str):
    if not WX_APP_TOKEN or not WX_UID:
        return
    uids = [u.strip() for u in WX_UID.split(",") if u.strip()]
    payload = {"appToken": WX_APP_TOKEN, "content": content,
               "summary": title, "contentType": 1, "uids": uids}
    try:
        req = Request(
            "https://wxpusher.zjiecode.com/api/send/message",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req, timeout=15):
            log("WxPusher 推送成功")
    except Exception as e:
        warn(f"WxPusher 推送失败: {e}")

def notify(title: str, content: str):
    send_tg(f"{title}\n\n{content}")
    send_wx(title, content)

# ── 截图 ──────────────────────────────────────────────────
def snap(sb, name: str) -> str | None:
    try:
        path = str(SCREENSHOT_DIR / f"{name}.png")
        sb.save_screenshot(path)
        log(f"截图: {path}")
        return path
    except Exception as e:
        warn(f"截图失败: {e}")
        return None

# ── 录屏 ──────────────────────────────────────────────────
class ScreenRecorder:
    """每 N 秒截一帧，结束后用 ffmpeg 合成 MP4。"""

    def __init__(self, sb, interval: float = 2.0):
        self.sb       = sb
        self.interval = interval
        self._frames: list[Path] = []
        self._running = False
        self._thread: threading.Thread | None = None
        self._idx = 0

    def start(self):
        if not ENABLE_RECORDING:
            return
        self._running = True
        self._thread  = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        log("🎬 录屏已启动（每 2 秒截一帧）")

    def _loop(self):
        while self._running:
            try:
                path = REC_FRAME_DIR / f"rec_{self._idx:04d}.png"
                self.sb.save_screenshot(str(path))
                self._frames.append(path)
                self._idx += 1
            except Exception:
                pass
            time.sleep(self.interval)

    def stop(self, output_name: str = "run") -> str | None:
        if not ENABLE_RECORDING:
            return None
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        if not self._frames:
            warn("录屏：没有帧，跳过视频合成")
            return None
        return self._compile(output_name)

    def _compile(self, output_name: str) -> str | None:
        if not shutil.which("ffmpeg"):
            warn("ffmpeg 未安装，跳过视频合成（帧已保留在 screenshots/rec/）")
            return None

        concat_file = RECORDING_DIR / "frames.txt"
        with open(concat_file, "w") as f:
            for p in self._frames:
                f.write(f"file '{p.resolve()}'\n")
                f.write(f"duration {self.interval}\n")
            if self._frames:
                f.write(f"file '{self._frames[-1].resolve()}'\n")

        out = RECORDING_DIR / f"{output_name}.mp4"
        cmd = [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(concat_file),
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-r", "10",
            str(out),
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if result.returncode == 0:
                log(f"🎬 视频已生成: {out}")
                return str(out)
            else:
                warn(f"ffmpeg 失败:\n{result.stderr[-500:]}")
                return None
        except Exception as e:
            warn(f"ffmpeg 异常: {e}")
            return None

# ── 等待 URL 关键字 ────────────────────────────────────────
def wait_for_url(sb, keyword: str, timeout: int = 20) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if keyword in sb.get_current_url():
            return True
        time.sleep(0.5)
    return False

# ── 登录 OuiHeberg OAuth ──────────────────────────────────
def login(sb):
    log("打开 OuiPanel 登录页...")
    sb.uc_open_with_reconnect(f"{BASE_URL}/login", reconnect_time=3)
    time.sleep(3)

    if "dash.ouipanel.com" in sb.get_current_url() and "/login" not in sb.get_current_url():
        log("已登录，跳过")
        return

    log("点击 OuiHeberg 登录按钮...")
    clicked = False
    for sel in [
        'button:contains("OuiHeberg")',
        'a:contains("OuiHeberg")',
        'button:contains("Connexion")',
    ]:
        try:
            if sb.is_element_visible(sel):
                sb.uc_click(sel)
                clicked = True
                log(f"已点击: {sel}")
                break
        except Exception:
            continue

    if not clicked:
        r = sb.execute_script("""
            var btns = document.querySelectorAll('button, a');
            for (var i = 0; i < btns.length; i++) {
                var t = (btns[i].innerText || '').toLowerCase();
                if (t.includes('ouiheberg') || t.includes('connexion')) {
                    btns[i].click();
                    return 'js:' + btns[i].innerText.trim();
                }
            }
            return 'not_found';
        """)
        if "not_found" not in str(r):
            clicked = True
            log(f"JS 点击: {r}")

    if not clicked:
        snap(sb, "login-btn-not-found")
        raise RuntimeError("未找到 OuiHeberg 登录按钮")

    if not wait_for_url(sb, "manager.ouiheberg.com", timeout=15):
        snap(sb, "oauth-redirect-failed")
        raise RuntimeError(f"未跳转到 OAuth 页，当前: {sb.get_current_url()}")

    log("填写邮箱和密码...")
    time.sleep(2)

    try:
        sb.type('input[type="email"], input[name="email"], #email', EMAIL)
    except Exception:
        sb.execute_script(
            "var i=document.querySelector('input[type=\"email\"],input[name=\"email\"],#email');"
            f"if(i){{i.value='{EMAIL}';i.dispatchEvent(new Event('input'));}}"
        )
    time.sleep(0.5)

    try:
        sb.type('input[type="password"], input[name="password"], #password', PASSWORD)
    except Exception:
        sb.execute_script(
            "var i=document.querySelector('input[type=\"password\"],input[name=\"password\"],#password');"
            f"if(i){{i.value='{PASSWORD}';i.dispatchEvent(new Event('input'));}}"
        )
    time.sleep(0.5)

    log("提交登录表单...")
    for sel in ['button:contains("Connexion")', 'button[type="submit"]']:
        try:
            if sb.is_element_visible(sel):
                sb.uc_click(sel)
                break
        except Exception:
            continue
    else:
        sb.execute_script(
            "var b=document.querySelector('button[type=\"submit\"],input[type=\"submit\"]');"
            "if(b)b.click();"
        )

    if not wait_for_url(sb, "ouipanel.com", timeout=20):
        snap(sb, "login-failed")
        raise RuntimeError(f"登录失败，当前: {sb.get_current_url()}")

    log(f"✅ 登录成功！当前: {sb.get_current_url()}")
    snap(sb, "01-after-login")

# ── 读取电源状态 ───────────────────────────────────────────
def get_power_status(sb) -> str:
    console_url = f"{BASE_URL}/server/{SERVER_ID}/console"
    log(f"打开控制台: {console_url}")
    sb.uc_open_with_reconnect(console_url, reconnect_time=2)
    time.sleep(5)

    sb.execute_script("""
        document.querySelectorAll('button').forEach(function(b){
            var t=(b.innerText||'').trim().toLowerCase();
            if(t==='ok'||t==='close'||t==='got it'||t==='dismiss') b.click();
        });
    """)
    time.sleep(1)
    snap(sb, "02-console")

    status = sb.execute_script("""
        var btns = document.querySelectorAll('button');
        var hasStart = false, hasStop = false;
        for (var i = 0; i < btns.length; i++) {
            var t = (btns[i].innerText || '').trim().toLowerCase();
            if (t === 'start')  hasStart = true;
            if (t === 'stop')   hasStop  = true;
        }
        if (hasStop)  return 'running';
        if (hasStart) return 'offline';
        return 'unknown';
    """)
    log(f"电源状态: {status}")
    return status or "unknown"

# ── 点击 Start ─────────────────────────────────────────────
def start_server(sb) -> bool:
    log("点击 Start 按钮...")
    for sel in ['button:contains("Start")']:
        try:
            if sb.is_element_visible(sel):
                if sb.get_text(sel).strip().lower() == "start":
                    sb.uc_click(sel)
                    log("已点击 Start")
                    return True
        except Exception:
            continue

    r = sb.execute_script("""
        var btns = document.querySelectorAll('button');
        for (var i = 0; i < btns.length; i++) {
            if ((btns[i].innerText||'').trim().toLowerCase() === 'start') {
                btns[i].click();
                return 'clicked';
            }
        }
        return 'not_found';
    """)
    if "not_found" not in str(r):
        log(f"JS Start: {r}")
        return True

    snap(sb, "start-btn-not-found")
    warn("未找到 Start 按钮")
    return False

# ── 主流程 ────────────────────────────────────────────────
def run():
    if not EMAIL or not PASSWORD:
        raise RuntimeError("缺少：OUIHEBERG_EMAIL 或 OUIHEBERG_PASSWORD")
    if not SERVER_ID:
        raise RuntimeError("缺少：OUIHEBERG_SERVER_ID")

    if UPTIME_STATUS == "up":
        log("✅ Uptime Kuma 状态 UP，服务器已恢复，退出")
        return

    log(f"▶ 检查服务器 [{SERVER_ID}]")
    if ENABLE_RECORDING:
        log("🎬 录屏已启用")
    else:
        log("📷 仅截图模式（录屏未启用）")

    driver = Driver(
        uc=True,
        headless=False,
        undetectable=True,
        chromium_arg="--no-sandbox,--disable-dev-shm-usage,--disable-gpu",
    )

    with driver as sb:
        recorder = ScreenRecorder(sb, interval=2.0)
        recorder.start()

        try:
            login(sb)

            power = get_power_status(sb)

            if power == "running":
                log("✅ 服务器运行中，无需操作")

            elif power in ("offline", "stopped", "unknown"):
                if power == "unknown":
                    warn("状态未知，尝试查找 Start 按钮...")
                else:
                    log("🔴 服务器离线，执行启动...")

                ok = start_server(sb)
                if not ok:
                    notify("❌ OuiHeberg 启动失败",
                           f"服务器 {SERVER_ID} 离线，但未找到 Start 按钮，请手动处理。")
                    return

                time.sleep(5)
                snap(sb, "03-after-start")

                # 轮询确认上线（最多 3 分钟）
                final = "unknown"
                for i in range(18):
                    time.sleep(10)
                    sb.refresh()
                    time.sleep(4)
                    final = sb.execute_script("""
                        var btns = document.querySelectorAll('button');
                        for (var i = 0; i < btns.length; i++) {
                            var t = (btns[i].innerText||'').trim().toLowerCase();
                            if (t === 'stop')  return 'running';
                            if (t === 'start') return 'offline';
                        }
                        return 'unknown';
                    """) or "unknown"
                    log(f"  等待上线 [{i+1}/18]: {final}")
                    if final == "running":
                        break

                snap(sb, "04-final")
                if final == "running":
                    log("✅ 服务器已上线")
                    notify("🚀 OuiHeberg 服务器已上线",
                           f"服务器 {SERVER_ID} 检测到离线，已自动执行 Start，现已 ONLINE。")
                else:
                    log(f"⚠️ 启动后状态为 {final}，请手动确认")
                    notify("⚠️ OuiHeberg 服务器启动中",
                           f"已发送 Start 指令，当前状态：{final}，请稍后手动确认。")

        except Exception as e:
            err(f"异常: {e}")
            traceback.print_exc()
            snap(sb, "error")
            notify("❌ OuiHeberg 监控脚本异常", str(e))
            recorder.stop("run-error")
            sys.exit(1)

        finally:
            video = recorder.stop("run")
            if video:
                log(f"🎬 录屏保存于: {video}")

    log("▶ 完成")


if __name__ == "__main__":
    try:
        run()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
