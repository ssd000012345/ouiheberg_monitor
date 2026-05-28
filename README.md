# ouiheberg_monitor

> 🚨 OuiHeberg / OuiPanel 服务器自动重启监控工具
>
> 通过 Uptime Kuma Webhook 触发 GitHub Actions，自动检测并唤醒离线的 OuiHeberg 托管服务器。

---

## 目录

- [项目简介](#项目简介)
- [工作原理](#工作原理)
- [项目结构](#项目结构)
- [前置要求](#前置要求)
- [配置步骤](#配置步骤)
  - [1. Fork 并配置仓库 Secrets](#1-fork-并配置仓库-secrets)
  - [2. 配置 Uptime Kuma Webhook](#2-配置-uptime-kuma-webhook)
  - [3. 手动测试](#3-手动测试)
- [环境变量说明](#环境变量说明)
- [功能详解](#功能详解)
  - [登录流程](#登录流程)
  - [服务器状态检测](#服务器状态检测)
  - [自动启动逻辑](#自动启动逻辑)
  - [TCP 连通检测](#tcp-连通检测)
  - [截图与录屏](#截图与录屏)
  - [通知推送](#通知推送)
  - [敏感信息遮盖](#敏感信息遮盖)
- [GitHub Actions 工作流](#github-actions-工作流)
- [通知渠道配置](#通知渠道配置)
  - [Telegram](#telegram)
  - [WxPusher（微信）](#wxpusher微信)
- [截图与录屏产物](#截图与录屏产物)
- [常见问题](#常见问题)
- [注意事项](#注意事项)

---

## 项目简介

`ouiheberg_monitor` 是一个基于 GitHub Actions + Selenium 的自动化监控工具，专为托管在 [OuiHeberg](https://ouiheberg.com) 上的 Minecraft（或其他类型）服务器设计。

当 Uptime Kuma 检测到服务器离线时，会通过 Webhook 触发本仓库的 GitHub Actions 工作流，工作流自动完成以下操作：

1. 使用 Selenium 登录 OuiPanel 控制台
2. 检测服务器当前电源状态
3. 如果服务器离线，自动点击 **Start** 按钮
4. 等待服务器上线（最长等待约 3 分钟）
5. 通过 Telegram / WxPusher 推送结果通知

整个过程无需人工干预，即使你在睡觉，服务器也能自动恢复。

---

## 工作原理

```
Uptime Kuma 检测到服务器离线
        │
        │  HTTP POST Webhook
        ▼
GitHub Actions (repository_dispatch: server-down)
        │
        │  启动 Ubuntu Runner
        ▼
安装依赖 + 启动 Xvfb 虚拟显示 + 启用 Cloudflare WARP
        │
        ▼
watchdog.py
  ├─ TCP 检测（可选）
  ├─ Selenium 登录 OuiPanel（OAuth 流程）
  ├─ 读取控制台电源状态（按钮 + 文案 + TCP 三重投票）
  ├─ 离线 → 点击 Start
  ├─ 轮询等待上线（最多 18 次 × 10 秒）
  └─ 推送结果通知（Telegram / WxPusher）
        │
        ▼
上传截图 / 录屏产物到 GitHub Actions Artifacts
```

---

## 项目结构

```
ouiheberg_monitor/
├── watchdog.py                        # 核心监控脚本
└── .github/
    └── workflows/
        └── mc-start-only.yml          # GitHub Actions 工作流定义
```

---

## 前置要求

- 一个 GitHub 账号，并将本仓库 Fork 到你自己的账号下
- 一个正在运行的 [Uptime Kuma](https://github.com/louislam/uptime-kuma) 实例
- OuiHeberg 账号（邮箱 + 密码）
- 需要监控的服务器的 OuiPanel Server ID
- （可选）Telegram Bot Token + Chat ID，用于消息推送
- （可选）WxPusher AppToken + UID，用于微信消息推送
- （可选）服务器的 TCP 地址（`host:port`），用于端口连通性辅助检测

---

## 配置步骤

### 1. Fork 并配置仓库 Secrets

Fork 本仓库后，进入 **Settings → Secrets and variables → Actions → New repository secret**，添加以下 Secrets：

| Secret 名称              | 说明                                       | 是否必填 |
|--------------------------|--------------------------------------------|----------|
| `OUIHEBERG_EMAIL`        | OuiHeberg 登录邮箱                         | ✅ 必填  |
| `OUIHEBERG_PASSWORD`     | OuiHeberg 登录密码                         | ✅ 必填  |
| `OUIHEBERG_SERVER_ID`    | OuiPanel 服务器 ID（见下方获取方式）       | ✅ 必填  |
| `TG_BOT_TOKEN`           | Telegram Bot Token                         | ⚪ 可选  |
| `TG_CHAT_ID`             | Telegram Chat ID（你的用户 ID 或群组 ID）  | ⚪ 可选  |
| `WX_APP_TOKEN`           | WxPusher 应用 Token                        | ⚪ 可选  |
| `WX_UID`                 | WxPusher UID（多个用英文逗号分隔）         | ⚪ 可选  |
| `SERVER_HOST_PORT`       | 服务器 TCP 地址，格式：`host:port`         | ⚪ 可选  |

**获取 `OUIHEBERG_SERVER_ID` 的方法：**

登录 OuiPanel 后，进入服务器控制台页面，观察浏览器地址栏：

```
https://dash.ouipanel.com/server/{SERVER_ID}/console
                                  ^^^^^^^^^^
                                  这就是 SERVER_ID
```

---

### 2. 配置 Uptime Kuma Webhook

> Uptime Kuma 是开源监控工具，需要部署在独立服务器上（推荐 Oracle Cloud 永久免费 VPS）。

#### 获取 GitHub Personal Access Token

1. 打开 [github.com/settings/tokens](https://github.com/settings/tokens) → **Generate new token (classic)**
2. 勾选 **`repo`** 权限（包含 Actions 写权限）
3. 生成并复制 Token（只显示一次）

#### 配置 Webhook 通知

**Settings → Notifications → Add Notification：**

| 字段 | 填写内容 |
|------|---------|
| 通知类型 | `Webhook` |
| 显示名称 | `GitHub Actions 紧急启动` |
| Post URL | `https://api.github.com/repos/你的用户名/ouiheberg_monitor/dispatches` |
| 请求体 | 选 `自定义内容` |

**请求体内容：**
```json
{
  "event_type": "server-down",
  "client_payload": {
    "reason": "Uptime Kuma alert",
    "heartbeat": "{{heartbeatJSON}}"
  }
}
```

**打开「额外 Header」开关，填入：**
```json
{
  "Authorization": "Bearer github_pat_你的token",
  "Accept": "application/vnd.github+json",
  "X-GitHub-Api-Version": "2022-11-28"
}
```

点 **测试**，去 GitHub Actions 页面确认出现新的运行记录。

#### 配置服务器监控项

**Add New Monitor：**

| 字段 | 填写内容 |
|------|---------|
| 监控类型 | `TCP Port` |
| 显示名称 | `OuiHeberg 监控` |
| 主机名 | 你的服务器地址（在 OuiPanel 控制台查看） |
| 端口 | 你的服务器实际端口 |
| 心跳间隔 | `60`（秒） |
| 重试次数 | `2` |
| 连续失败时重复发送通知的间隔次数 | `9999`（防止重复触发） |
| 通知 | 选刚配置的 `GitHub Actions 紧急启动` |

---

### 3. 手动测试

在 GitHub Actions 页面，找到工作流 `🚨 服务器离线紧急启动（Uptime Kuma 触发）`，点击 **Run workflow** 手动触发：

- **enable_recording**：是否启用录屏（`true` = 录屏，`false` = 仅截图，默认 `false`）
- **reason**：手动触发原因备注（用于日志记录）

---

## 环境变量说明

以下环境变量由 GitHub Actions 工作流从 Secrets 中注入，`watchdog.py` 通过 `os.environ` 读取：

| 变量名               | 类型   | 说明                                           |
|----------------------|--------|------------------------------------------------|
| `OUIHEBERG_EMAIL`    | string | OuiHeberg 登录邮箱                             |
| `OUIHEBERG_PASSWORD` | string | OuiHeberg 登录密码                             |
| `OUIHEBERG_SERVER_ID`| string | 目标服务器 ID                                  |
| `TG_BOT_TOKEN`       | string | Telegram Bot Token（可选）                     |
| `TG_CHAT_ID`         | string | Telegram 接收通知的 Chat ID（可选）            |
| `WX_APP_TOKEN`       | string | WxPusher 应用 Token（可选）                    |
| `WX_UID`             | string | WxPusher 用户 UID，多个用逗号分隔（可选）      |
| `SERVER_HOST_PORT`   | string | 服务器 TCP 地址，如 `1.2.3.4:25565`（可选）    |
| `UPTIME_STATUS`      | string | Uptime Kuma 传入的状态（`up` / `down`）        |
| `UPTIME_HEARTBEAT`   | JSON   | Uptime Kuma 传入的心跳详情                     |
| `ENABLE_RECORDING`   | string | 是否启用录屏（`true` / `false`，默认 `false`） |
| `DISPLAY`            | string | 虚拟显示地址（由工作流设置为 `:99`）           |

---

## 功能详解

### 登录流程

脚本使用 [SeleniumBase](https://seleniumbase.io/) 的 UC（Undetected Chrome）模式访问 OuiPanel，流程如下：

1. 打开 `https://dash.ouipanel.com/login`
2. 查找并点击 **OuiHeberg** 登录按钮（支持文字识别和 JS 降级点击）
3. 等待页面跳转至 OuiHeberg OAuth 域名（`manager.ouiheberg.com`）
4. 填写邮箱和密码（支持 Selenium 输入和 JS 直接赋值两种方式）
5. 提交表单，等待重定向回 OuiPanel
6. 登录成功后保存截图 `01-after-login.png`

---

### 服务器状态检测

访问控制台页面 `https://dash.ouipanel.com/server/{SERVER_ID}/console` 后，通过三种方式综合判断服务器状态：

| 检测方式     | 说明                                                       |
|--------------|------------------------------------------------------------|
| **按钮检测** | 检测 `Start`（绿色按钮可用）和 `Stop`（红色按钮可用）的状态 |
| **文案检测** | 检测页面文字中是否含 `server is currently offline` 等关键词 |
| **TCP 检测** | 如配置了 `SERVER_HOST_PORT`，直接尝试 TCP 连接端口         |

三种检测结果采用**投票机制**：离线票数 > 在线票数则判定为离线；如三种结果均不明确，保守判定为离线。

---

### 自动启动逻辑

检测到服务器离线后：

1. 通过 JS 精准定位并点击 `Start` 按钮（`btn-outline-success` 样式 + 未禁用）
2. 点击后保存截图 `03-after-start.png`
3. **轮询等待上线**：每 10 秒刷新页面，检测 TCP 连通性或按钮状态
   - 最多等待 **18 次 × 10 秒 ≈ 3 分钟**
   - TCP 可达则立即认定为 `running`
4. 保存最终截图 `04-final.png`
5. 根据最终状态发送通知

---

### TCP 连通检测

如配置了环境变量 `SERVER_HOST_PORT`（格式：`host:port`），脚本会在以下时机进行 TCP 连通测试：

- 初次状态判断
- 每次轮询等待期间

TCP 检测的优先级高于按钮和文案检测，一旦端口可达即认定服务器 `running`，端口不可达则认定为 `offline`。

---

### 截图与录屏

**截图**（始终启用）：

| 文件名                   | 拍摄时机             |
|--------------------------|----------------------|
| `01-after-login.png`     | 登录成功后           |
| `02-console.png`         | 控制台页面加载后     |
| `03-after-start.png`     | 点击 Start 5 秒后    |
| `04-final.png`           | 轮询结束后           |
| `error.png`              | 脚本发生异常时       |

**录屏**（需手动开启 `ENABLE_RECORDING=true`）：

- 每 2 秒截取一帧，存储于 `screenshots/rec/`
- 流程结束后使用 `ffmpeg` 合成为 `recordings/run.mp4`（H.264 / 10fps）

所有产物均会上传至 GitHub Actions Artifacts，保留 7 天，可在工作流运行记录页面下载。

---

### 通知推送

脚本支持两种推送渠道，可同时启用：

**触发通知的场景：**

| 场景                           | 通知标题                          |
|--------------------------------|-----------------------------------|
| Start 按钮未找到               | `❌ OuiHeberg 启动失败`           |
| 服务器已成功上线               | `🚀 OuiHeberg 服务器已上线`       |
| 启动指令已发送但状态未确认     | `⚠️ OuiHeberg 服务器启动中`       |
| 脚本发生未捕获异常             | `❌ OuiHeberg 监控脚本异常`       |

---

### 敏感信息遮盖

截图前，脚本会通过 JavaScript 向页面中注入黑色覆盖层，自动遮盖以下信息：

- 登录邮箱地址（及用户名部分）
- 服务器 IP 地址和端口号

遮盖采用精确文本节点定位 + 固定定位覆盖块实现，截图完成后自动移除覆盖层，不影响后续操作。

> 注意：录屏帧**不做**敏感信息遮盖，如需公开录屏文件请自行处理。

---

## GitHub Actions 工作流

工作流文件：`.github/workflows/mc-start-only.yml`

**触发条件：**

| 触发方式               | 说明                                               |
|------------------------|----------------------------------------------------|
| `repository_dispatch`  | Uptime Kuma Webhook 触发，`event_type: server-down` |
| `workflow_dispatch`    | 手动在 GitHub Actions 页面触发，支持参数配置        |

**工作流步骤：**

1. 拉取仓库代码
2. 安装 Python 3.12
3. 安装 Python 依赖（`seleniumbase`、`pillow`）
4. 安装系统依赖（Chrome 运行库、Xvfb、ffmpeg）
5. 启动 Xvfb 虚拟显示（`:99`，分辨率 1280×800）
6. 启用 Cloudflare WARP（绕过可能的 IP 封锁）
7. 修复 DNS 配置（使用 `1.1.1.1` / `8.8.8.8`）
8. 打印触发信息（触发方式、原因、录屏模式）
9. 运行 `watchdog.py`
10. 上传截图产物（`screenshots/`）
11. 上传录屏产物（`recordings/`）
12. 清理旧运行记录（仅保留最新 3 条）

**运行超时：** 15 分钟

---

## 通知渠道配置

### Telegram

1. 通过 [@BotFather](https://t.me/BotFather) 创建 Bot，获取 `Token`
2. 向 Bot 发送一条消息，然后访问：
   ```
   https://api.telegram.org/bot{TOKEN}/getUpdates
   ```
   从返回 JSON 中找到 `chat.id` 字段，即为 `TG_CHAT_ID`
3. 将 Token 和 Chat ID 分别配置为 `TG_BOT_TOKEN` 和 `TG_CHAT_ID` Secrets

### WxPusher（微信）

1. 前往 [wxpusher.zjiecode.com](https://wxpusher.zjiecode.com) 注册并创建应用
2. 获取 **AppToken** 配置为 `WX_APP_TOKEN`
3. 关注应用后获取 **UID** 配置为 `WX_UID`
4. 多个接收人的 UID 用英文逗号分隔，例如：`UID_abc123,UID_def456`

---

## 截图与录屏产物

每次工作流运行结束后，可在 GitHub Actions 运行记录页面的 **Artifacts** 区域下载：

| 产物名                       | 内容                         | 保留时间 |
|------------------------------|------------------------------|----------|
| `screenshots-{run_number}`   | 关键节点截图（PNG）          | 7 天     |
| `recording-{run_number}`     | 录屏视频（MP4，需开启录屏）  | 7 天     |

---

## 常见问题

**Q：工作流触发了但服务器没有被启动？**

检查以下几点：
- Secrets 是否正确配置，尤其是 `OUIHEBERG_SERVER_ID`
- OuiPanel 是否修改了 UI 结构，导致按钮选择器失效
- 查看工作流日志中的 `[INFO]` / `[WARN]` / `[ERROR]` 输出
- 下载截图产物，直观查看脚本执行到哪一步

**Q：`SERVER_HOST_PORT` 填什么？**

填写你的 Minecraft 服务器地址，格式为 `IP:端口`，例如：`123.45.67.89:25565`。配置后可提高状态检测的准确性，即使 OuiPanel UI 有变化也能正确判断。

**Q：Cloudflare WARP 是必须的吗？**

不是必须的，但如果 GitHub Actions 的 IP 被 OuiHeberg 封锁，启用 WARP 可以切换出口 IP 绕过限制。

**Q：如何确认 Uptime Kuma Webhook 格式正确？**

可以在 GitHub Actions 页面手动触发工作流（`workflow_dispatch`）进行测试，确认脚本能正常执行后再关联 Uptime Kuma。

---

## 注意事项

- 本工具使用 Selenium 自动化操作浏览器，依赖 OuiPanel 的页面结构。若 OuiPanel 更新 UI，选择器可能需要相应调整。
- 请勿将包含 Secrets 的截图或录屏公开分享，尽管截图已做敏感信息遮盖，但录屏帧未做处理。
- GitHub Actions 免费额度（公开仓库免费，私有仓库每月 2000 分钟），监控触发频率不高时一般不会超限。
- 工作流每次运行最长 15 分钟，旧记录自动清理，仅保留最新 3 条，不会积累大量历史记录。
