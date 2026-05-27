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
import socket
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

# TCP 连通性检测：格式 "host:port"，例如 "88.151.197.15:8326"
SERVER_HOST_PORT = os.environ.get("SERVER_HOST_PORT", "").strip()

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

# ── TCP 连通性检测 ─────────────────────────────────────────
def check_tcp(host: str, port: int, timeout: float = 5.0) -> bool:
    """尝试 TCP 连接，返回 True 表示端口可达（服务器真正在线）。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (socket.timeout, ConnectionRefusedError, OSError):
        return False

def tcp_is_online() -> bool | None:
    """
    若配置了 SERVER_HOST_PORT 则进行 TCP 检测，返回 True/False。
    未配置则返回 None（跳过 TCP 检测）。
    """
    if not SERVER_HOST_PORT:
        return None
    try:
        host, port_str = SERVER_HOST_PORT.rsplit(":", 1)
        port = int(port_str)
    except ValueError:
        warn(f"SERVER_HOST_PORT 格式错误（应为 host:port）: {SERVER_HOST_PORT}")
        return None
    result = check_tcp(host, port)
    # 日志中隐藏真实地址
    log(f"TCP 连通检测 [***:***]: {'✅ 可达' if result else '❌ 不可达'}")
    return result

# ── 敏感信息涂抹 ──────────────────────────────────────────
try:
    from PIL import Image, ImageDraw, ImageFont
    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False
    warn("Pillow 未安装，截图涂抹功能不可用（pip install pillow）")

# 需要涂抹的敏感字符串列表（运行时填充）
_SENSITIVE_STRINGS: list[str] = []

def _build_sensitive_list():
    """收集所有敏感字符串（邮箱、IP、端口）。"""
    items = []
    if EMAIL:
        items.append(EMAIL)
        # 也单独加本地部分（@ 前）防止部分显示
        local = EMAIL.split("@")[0]
        if local:
            items.append(local)
    if SERVER_HOST_PORT:
        items.append(SERVER_HOST_PORT)
        # 单独加 IP 和端口
        try:
            host, port_str = SERVER_HOST_PORT.rsplit(":", 1)
            items.append(host)
            items.append(port_str)
        except ValueError:
            pass
    return [s for s in items if s]

def mask_screenshot(path: str) -> str:
    """
    对截图中的敏感文字区域进行黑色矩形涂抹。
    使用 pytesseract OCR 定位文字位置；若 OCR 不可用则跳过。
    同时对已知 UI 模式（邮箱行）进行区域涂抹。
    返回处理后的文件路径（覆盖原文件）。
    """
    if not _PIL_AVAILABLE:
        return path
    if not _SENSITIVE_STRINGS:
        return path

    try:
        img = Image.open(path).convert("RGB")
        draw = ImageDraw.Draw(img)

        # 尝试用 pytesseract 精准定位
        try:
            import pytesseract
            data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
            n = len(data["text"])
            for i in range(n):
                word = (data["text"][i] or "").strip()
                if not word:
                    continue
                for sensitive in _SENSITIVE_STRINGS:
                    if word.lower() in sensitive.lower() or sensitive.lower() in word.lower():
                        x, y, w, h = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
                        # 适当扩展涂抹范围
                        pad = 4
                        draw.rectangle([x - pad, y - pad, x + w + pad, y + h + pad], fill="black")
                        break
        except Exception:
            # OCR 不可用时，对整行区域涂抹（保守策略）
            pass

        # ── 保守兜底：扫描图片中与敏感信息匹配的宽行区域 ──
        # 对含有 @ 符号的邮箱，涂抹页面顶部可能出现账号的区域
        # 这是一个启发式策略：登录后邮箱通常显示在固定位置
        w_img, h_img = img.size
        # 涂抹左侧边栏账号区（OuiPanel 布局：左上角头像+邮箱）
        # 根据截图观察，账号信息大约在 y=250~290 区间
        if EMAIL:
            draw.rectangle([0, 240, 420, 300], fill=(20, 20, 20))  # 左侧面板账号行

        # 涂抹控制台顶部标题中的邮箱（h6 class="modern-card-header-title"）
        # 根据截图，Console: xxx@xxx.com 出现在控制台卡片标题处
        # 大约在 y=335~365（基于 832px 高度截图）
        draw.rectangle([440, 330, 980, 370], fill=(20, 20, 20))  # 控制台卡片标题行

        img.save(path)
        log(f"🔒 截图已涂抹敏感信息: {path}")
    except Exception as e:
        warn(f"截图涂抹失败: {e}")

    return path

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

# ── 截图（含涂抹） ─────────────────────────────────────────
def snap(sb, name: str) -> str | None:
    try:
        path = str(SCREENSHOT_DIR / f"{name}.png")
        sb.save_screenshot(path)
        log(f"截图: {path}")
        mask_screenshot(path)  # 涂抹敏感信息
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
                # 录屏帧也做涂抹
                mask_screenshot(str(path))
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
    """
    综合判断服务器状态：
    1. 优先使用 TCP 连通性检测（最可靠）
    2. 检测页面按钮（Stop→running / Start→offline）
    3. 检测 SVG 图标颜色/data-testid（StopCircleIcon→running，PlayCircleIcon→offline）
    4. 检测控制台离线文案
    只有以上多项一致指向 running 时才返回 running。
    """
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

    # ── 方法1：按钮文本检测 ──────────────────────────────
    btn_status = sb.execute_script("""
        var btns = document.querySelectorAll('button');
        var hasStart = false, hasStop = false;
        for (var i = 0; i < btns.length; i++) {
            var t = (btns[i].innerText || '').trim().toLowerCase();
            if (t === 'start')  hasStart = true;
            if (t === 'stop')   hasStop  = true;
        }
        if (hasStop && !hasStart)  return 'running';
        if (hasStart && !hasStop)  return 'offline';
        if (hasStop && hasStart)   return 'ambiguous';
        return 'unknown';
    """)

    # ── 方法2：SVG 图标 data-testid 检测 ────────────────
    # StopCircleIcon（红色圆形停止图标）= 服务器离线（可以停止）? 不对
    # 根据截图分析：红色圆圈图标出现时服务器是 OFFLINE 状态
    # OuiPanel 用 StopCircleIcon 表示"可以停止" = running
    # PlayCircleIcon / 绿色播放图标 = 可以启动 = offline
    icon_status = sb.execute_script("""
        // 检测 data-testid
        var stop_icon  = document.querySelector('[data-testid="StopCircleIcon"]');
        var play_icon  = document.querySelector('[data-testid="PlayCircleIcon"]');
        var start_icon = document.querySelector('[data-testid="PlayArrowIcon"]');

        if (stop_icon && !play_icon && !start_icon) return 'running';
        if ((play_icon || start_icon) && !stop_icon) return 'offline';

        // 检测控制台卡片头部图标颜色（红色=停止中=offline，绿色=运行中）
        var cardIcon = document.querySelector('.modern-card-header-icon svg, .card-header svg');
        if (cardIcon) {
            var fill = cardIcon.getAttribute('fill') || '';
            var style = cardIcon.getAttribute('style') || '';
            var color = cardIcon.style.color || '';
            if (fill.includes('red') || fill.includes('#f') || color.includes('red')) return 'offline_icon';
            if (fill.includes('green') || color.includes('green')) return 'running_icon';
        }
        return 'icon_unknown';
    """)

    # ── 方法3：控制台文案检测 ────────────────────────────
    console_text_status = sb.execute_script("""
        var consoleEl = document.querySelector('.xterm-rows, .console-output, pre, code');
        var text = consoleEl ? consoleEl.innerText.toLowerCase() : document.body.innerText.toLowerCase();
        if (text.includes('server is currently offline')) return 'offline';
        if (text.includes('server is running') || text.includes('done') || text.includes('started')) return 'running';
        return 'text_unknown';
    """)

    log(f"  按钮检测: {btn_status} | 图标检测: {icon_status} | 文案检测: {console_text_status}")

    # ── 方法4：TCP 连通性检测（最终裁决） ────────────────
    tcp_result = tcp_is_online()  # True / False / None

    # ── 综合判断 ─────────────────────────────────────────
    # TCP 检测结果最权威
    if tcp_result is True:
        log("✅ TCP 连通检测：端口可达，服务器在线")
        final_status = "running"
    elif tcp_result is False:
        log("❌ TCP 连通检测：端口不可达，服务器离线")
        final_status = "offline"
    else:
        # TCP 未配置，综合其他信号
        offline_votes = sum([
            btn_status in ("offline",),
            icon_status in ("offline", "offline_icon"),
            console_text_status in ("offline",),
        ])
        running_votes = sum([
            btn_status in ("running",),
            icon_status in ("running", "running_icon"),
            console_text_status in ("running",),
        ])

        log(f"  投票结果 → 离线: {offline_votes} | 在线: {running_votes}")

        if offline_votes > running_votes:
            final_status = "offline"
        elif running_votes > offline_votes:
            final_status = "running"
        else:
            # 平票或全部未知：保守判定为 offline 以触发启动
            warn("状态信号不明确，保守判定为 offline 以触发启动检查")
            final_status = "offline"

    log(f"电源状态: {final_status}")
    return final_status

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
    global _SENSITIVE_STRINGS
    _SENSITIVE_STRINGS = _build_sensitive_list()

    if not EMAIL or not PASSWORD:
        raise RuntimeError("缺少：OUIHEBERG_EMAIL 或 OUIHEBERG_PASSWORD")
    if not SERVER_ID:
        raise RuntimeError("缺少：OUIHEBERG_SERVER_ID")

    if UPTIME_STATUS == "up":
        # 即使 Uptime Kuma 报告 UP，也用 TCP 二次确认
        tcp_result = tcp_is_online()
        if tcp_result is False:
            warn("⚠️ Uptime Kuma 状态 UP 但 TCP 检测不可达，继续执行检查")
        elif tcp_result is True:
            log("✅ Uptime Kuma 状态 UP 且 TCP 可达，服务器已确认在线，退出")
            return
        else:
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

                # 轮询确认上线（最多 3 分钟），TCP + 按钮双重确认
                final = "unknown"
                for i in range(18):
                    time.sleep(10)
                    sb.refresh()
                    time.sleep(4)

                    # 先用 TCP 快速判断
                    tcp_check = tcp_is_online()
                    if tcp_check is True:
                        final = "running"
                        log(f"  等待上线 [{i+1}/18]: TCP 可达 → running")
                        break
                    elif tcp_check is False:
                        final = "offline"
                        log(f"  等待上线 [{i+1}/18]: TCP 不可达 → offline")
                        continue

                    # 无 TCP 配置时用按钮检测
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
