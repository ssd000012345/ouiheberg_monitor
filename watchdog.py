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
SERVER_HOST_PORT = os.environ.get("SERVER_HOST_PORT", "").strip()

_heartbeat_raw     = os.environ.get("UPTIME_HEARTBEAT", "").strip()
_uptime_status_raw = os.environ.get("UPTIME_STATUS", "").strip().lower()
try:
    _hb = json.loads(_heartbeat_raw) if _heartbeat_raw else {}
    UPTIME_STATUS = str(_hb.get("status", _uptime_status_raw)).lower()
    if UPTIME_STATUS == "1":  UPTIME_STATUS = "up"
    elif UPTIME_STATUS == "0": UPTIME_STATUS = "down"
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

# ── TCP 连통检测 ───────────────────────────────────────────
def check_tcp(host: str, port: int, timeout: float = 5.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (socket.timeout, ConnectionRefusedError, OSError):
        return False

def tcp_is_online() -> bool | None:
    if not SERVER_HOST_PORT:
        return None
    try:
        host, port_str = SERVER_HOST_PORT.rsplit(":", 1)
        port = int(port_str)
    except ValueError:
        warn("SERVER_HOST_PORT 格式错误（应为 host:port）")
        return None
    result = check_tcp(host, port)
    log(f"TCP 连通检测 [***:***]: {'✅ 可达' if result else '❌ 不可达'}")
    return result

# ── 敏感信息收集 ──────────────────────────────────────────
_SENSITIVE_STRINGS: list[str] = []

def _build_sensitive_list():
    items = []
    if EMAIL:
        items.append(EMAIL)
        local = EMAIL.split("@")[0]
        if local:
            items.append(local)
    if SERVER_HOST_PORT:
        items.append(SERVER_HOST_PORT)
        try:
            host, port_str = SERVER_HOST_PORT.rsplit(":", 1)
            items.append(host)
            items.append(port_str)
        except ValueError:
            pass
    return [s for s in items if s]

# ── JS注入：截图前在浏览器内遮盖敏感文字 ──────────────────
def inject_redaction(sb):
    """
    在页面中找到所有包含敏感信息的文本节点，
    用黑色 overlay div 覆盖，截图后再移除。
    这是最可靠的方案——直接在渲染层遮盖，不依赖像素坐标。
    """
    if not _SENSITIVE_STRINGS:
        return

    sensitive_json = json.dumps(_SENSITIVE_STRINGS)
    sb.execute_script(f"""
        (function() {{
            var sensitiveList = {sensitive_json};
            var overlays = [];

            function addOverlay(rect) {{
                var d = document.createElement('div');
                d.setAttribute('data-redact', 'true');
                d.style.cssText = [
                    'position:fixed',
                    'left:' + rect.left + 'px',
                    'top:' + rect.top + 'px',
                    'width:' + (rect.width + 8) + 'px',
                    'height:' + (rect.height + 4) + 'px',
                    'background:#111',
                    'z-index:2147483647',
                    'pointer-events:none'
                ].join(';');
                document.body.appendChild(d);
                overlays.push(d);
            }}

            // 遍历所有文本节点，找包含敏感词的
            var walker = document.createTreeWalker(
                document.body,
                NodeFilter.SHOW_TEXT,
                null
            );
            var node;
            while (node = walker.nextNode()) {{
                var text = node.nodeValue || '';
                var hit = sensitiveList.some(function(s) {{
                    return s && text.toLowerCase().includes(s.toLowerCase());
                }});
                if (!hit) continue;
                var range = document.createRange();
                range.selectNode(node);
                var rects = range.getClientRects();
                for (var i = 0; i < rects.length; i++) {{
                    var r = rects[i];
                    if (r.width > 0 && r.height > 0) {{
                        addOverlay(r);
                    }}
                }}
            }}

            // 额外：直接定位已知含敏感信息的元素
            var selectors = [
                'h6.modern-card-header-title',   // Console: xxx@xxx.com
                '.simplebar-content h6',          // 侧边栏账号名
                '.connection-info',               // Connection information
                '[class*="server-address"]',      // 服务器地址行
                '.card-body .text-muted'          // 可能含IP的文本
            ];
            selectors.forEach(function(sel) {{
                document.querySelectorAll(sel).forEach(function(el) {{
                    var text = el.innerText || '';
                    var hit = sensitiveList.some(function(s) {{
                        return s && text.toLowerCase().includes(s.toLowerCase());
                    }});
                    if (hit) {{
                        var rect = el.getBoundingClientRect();
                        if (rect.width > 0) addOverlay(rect);
                    }}
                }});
            }});

            window.__redactOverlays = overlays;
        }})();
    """)
    time.sleep(0.1)  # 等 overlay 渲染

def remove_redaction(sb):
    """移除之前注入的遮盖层。"""
    try:
        sb.execute_script("""
            (document.querySelectorAll('[data-redact="true"]') || [])
                .forEach(function(d){ d.remove(); });
            window.__redactOverlays = [];
        """)
    except Exception:
        pass

# ── 截图：注入遮盖 → 截图 → 移除遮盖（录屏帧不处理）──────
def snap(sb, name: str) -> str | None:
    """
    截取关键截图，截图前用 JS 覆盖敏感信息，截后恢复页面。
    只有命名截图（01/02/03/04）才做遮盖，录屏帧不处理。
    """
    try:
        path = str(SCREENSHOT_DIR / f"{name}.png")
        inject_redaction(sb)
        sb.save_screenshot(path)
        remove_redaction(sb)
        log(f"截图: {path}")
        return path
    except Exception as e:
        remove_redaction(sb)
        warn(f"截图失败: {e}")
        return None

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

# ── 录屏（帧不做任何涂抹处理）────────────────────────────
class ScreenRecorder:
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
            warn("ffmpeg 未安装，跳过视频合成")
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
            "ffmpeg", "-y", "-f", "concat", "-safe", "0",
            "-i", str(concat_file),
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "10",
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
    for sel in ['button:contains("OuiHeberg")', 'a:contains("OuiHeberg")', 'button:contains("Connexion")']:
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

    # 精准按钮检测（class + text + disabled）
    btn_status = sb.execute_script("""
        var startActive = false, stopActive = false;
        document.querySelectorAll('button').forEach(function(b) {
            var txt = (b.innerText || b.textContent || '').trim().toLowerCase();
            var cls = b.className || '';
            var dis = b.disabled || b.classList.contains('disabled');
            if (txt === 'start' && cls.includes('btn-outline-success') && !dis) startActive = true;
            if (txt === 'stop'  && cls.includes('btn-outline-danger')  && !dis) stopActive  = true;
        });
        if (stopActive  && !startActive) return 'running';
        if (startActive && !stopActive)  return 'offline';
        if (stopActive  && startActive)  return 'ambiguous';
        return 'unknown';
    """)

    console_text_status = sb.execute_script("""
        var text = document.body.innerText.toLowerCase();
        if (text.includes('server is currently offline')) return 'offline';
        if (text.includes('console commands must be used')) return 'running';
        return 'text_unknown';
    """)

    log(f"  按钮检测: {btn_status} | 文案检测: {console_text_status}")

    tcp_result = tcp_is_online()
    if tcp_result is True:
        log("✅ TCP：端口可达 → running")
        return "running"
    elif tcp_result is False:
        log("❌ TCP：端口不可达 → offline")
        return "offline"

    offline_votes = sum([btn_status == "offline", console_text_status == "offline"])
    running_votes = sum([btn_status == "running", console_text_status == "running"])
    log(f"  投票 → 离线: {offline_votes} | 在线: {running_votes}")

    if offline_votes > running_votes:  return "offline"
    if running_votes > offline_votes:  return "running"
    warn("状态信号不明确，保守判定为 offline")
    return "offline"

# ── 点击 Start ─────────────────────────────────────────────
def start_server(sb) -> bool:
    log("点击 Start 按钮...")
    r = sb.execute_script("""
        var found = null;
        document.querySelectorAll('button').forEach(function(b) {
            var txt = (b.innerText || b.textContent || '').trim().toLowerCase();
            var cls = b.className || '';
            var dis = b.disabled || b.classList.contains('disabled');
            if (txt === 'start' && cls.includes('btn-outline-success') && !dis) found = b;
        });
        if (found) { found.click(); return 'clicked:' + found.className; }
        return 'not_found';
    """)
    if "not_found" not in str(r):
        log(f"✅ JS 精准点击 Start: {r}")
        return True

    for sel in ['button:contains("Start")', '.btn-outline-success']:
        try:
            if sb.is_element_visible(sel):
                sb.uc_click(sel)
                log(f"已点击: {sel}")
                return True
        except Exception:
            continue

    snap(sb, "start-btn-not-found")
    warn("未找到可用的 Start 按钮")
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
        tcp_result = tcp_is_online()
        if tcp_result is False:
            warn("⚠️ Uptime Kuma 状态 UP 但 TCP 不可达，继续执行检查")
        elif tcp_result is True:
            log("✅ Uptime Kuma UP 且 TCP 可达，确认在线，退出")
            return
        else:
            log("✅ Uptime Kuma 状态 UP，退出")
            return

    log("▶ 检查服务器 [***]")
    log("🎬 录屏已启用" if ENABLE_RECORDING else "📷 仅截图模式")

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
                    notify("❌ OuiHeberg 启动失败", "服务器离线，但未找到 Start 按钮，请手动处理。")
                    return

                time.sleep(5)
                snap(sb, "03-after-start")

                final = "unknown"
                for i in range(18):
                    time.sleep(10)
                    sb.refresh()
                    time.sleep(4)

                    tcp_check = tcp_is_online()
                    if tcp_check is True:
                        final = "running"
                        log(f"  等待上线 [{i+1}/18]: TCP 可达 → running")
                        break
                    elif tcp_check is False:
                        final = "offline"
                        log(f"  等待上线 [{i+1}/18]: TCP 不可达")
                        continue

                    final = sb.execute_script("""
                        var startActive = false, stopActive = false;
                        document.querySelectorAll('button').forEach(function(b) {
                            var txt = (b.innerText||'').trim().toLowerCase();
                            var cls = b.className || '';
                            var dis = b.disabled || b.classList.contains('disabled');
                            if (txt==='start' && cls.includes('btn-outline-success') && !dis) startActive=true;
                            if (txt==='stop'  && cls.includes('btn-outline-danger')  && !dis) stopActive=true;
                        });
                        if (stopActive && !startActive) return 'running';
                        if (startActive && !stopActive) return 'offline';
                        return 'unknown';
                    """) or "unknown"
                    log(f"  等待上线 [{i+1}/18]: {final}")
                    if final == "running":
                        break

                snap(sb, "04-final")
                if final == "running":
                    log("✅ 服务器已上线")
                    notify("🚀 OuiHeberg 服务器已上线", "服务器检测到离线，已自动执行 Start，现已 ONLINE。")
                else:
                    log(f"⚠️ 启动后状态为 {final}，请手动确认")
                    notify("⚠️ OuiHeberg 服务器启动中", f"已发送 Start 指令，当前状态：{final}，请稍后手动确认。")

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
