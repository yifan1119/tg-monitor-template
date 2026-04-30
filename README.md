# TG Monitor — 多部门 TG 私聊监控模板

**Telegram 私聊监控系统**,专为业务审查/合规场景设计:监听外事号私聊、关键词预警、未回复提醒、删除消息溯源,全量落盘到 Google Sheets。一条命令装完 Docker + HTTPS + 后台,非技术同事也能部。

> 📌 **最新版**:v3.0.13(2026-04-30) — 🔧 **升级后 web 后台 502 自愈(共享 Caddy 模式)** — `update.sh` 末段加 network 重连自愈:检测 `tg-web` 跟它对应的共享 Caddy 不在同一 docker network → 自动 `docker network connect` + `caddy reload`。修一台 VPS 多部门 + 共享 Caddy 模式下,`docker compose up -d` recreate 容器后 `enable_https.sh` 旧 connect 丢失导致 Caddy DNS 解析失败的 502 问题。仅 shared 模式生效(`MY_CADDY != tg-caddy-${COMPANY_NAME}`),自建 Caddy 不影响;幂等 + 失败不阻断。⚠ 第一次升到 v3.0.13 仍需手工修一次,之后自动
> 之前:v3.1(2026-04-29) — 📋 **Sheet 后台扫描 + 客户删旧消息自动回填空位** — `peers` 加 `next_sheet_row` 缓存(migration V6),`write_messages` 双轨决策(update 命中 next_row / NULL 走 append fallback),`_sheet_position_resync_loop` 每 15 分钟 `ws.get_all_values` 一次性扫描整 worksheet 找首空行(1 API/ws 无视 peer 数),解决 v3.0.8 `values.append` 被 Google 自动检测全表 boundary 推高行号、客户删旧消息不回填的痛点;feature flag `SHEET_RESYNC_ENABLED` 默认 ON,关掉退回 v3.0.9 行为;tg-web → tg-monitor IPC 通过 `data/.sheet_resync_request` 文件标志(跟 ADR-0021 OAuth 同模式),admin 「立刻重扫」按钮触发
> 之前:v3.0.9(2026-04-29) — 📊 **中央台数据接口扩展 — 0 客户可见 UI 改动** — `dashboard_api.accounts_matrix()` SELECT 加 `tg_id / business_tg_id / owner_tg_id / remind_*_text`;`alerts_recent()` SELECT 加 `status / stage / keyword / reviewed_at / sheet_written / claimed_at / last_write_error + account_id/peer_id/msg_id`;新增 4 个 `/api/v1/*` 只读 endpoint(`violations` / `alerts` / `peers` / `messages`)沿用 metrics token 鉴权。0 新表 0 新字段 0 数据迁移,纯只读
> 之前:v3.0.8.3(2026-04-25) — 🔧 **修「立刻重启监听器」404 找不到容器** — `/api/restart` 改用 `_start_tg_monitor()` 复用现有 fallback(`.env COMPANY_NAME` 跟实际 docker 容器名对不齐时自动 fallback 到本机任意 tg-monitor-*);`dashboard_api._diagnose_sheets_stuck` 同样加 fallback。客户案例: URL `gs2` 但 `.env` 是 `gs1`(部署遗留 inconsistency)
> 之前:v3.0.8.2(2026-04-25) — 🔧 **升级提示去掉 SSH 包装 + 复制按钮 HTTP/HTTPS 三层兜底 + 深度诊断永远可见入口** — `upgrader.build_upgrade_cmd` 不再 wrap `ssh root@<IP>`(误导客户);3 个 templates 复制按钮加 `copyTextFallback`(`navigator.clipboard` → `execCommand('copy')` → `prompt()` 三层);驾驶舱日志面板上方新增「Sheet 写入诊断 ▸ 立刻深度诊断」**永远可见按钮**(admin only),客户随时点查未写明细 + 一键修
> 之前:v3.0.8.1(2026-04-25) — 🔧 **docker cp 漏同步根治 + 普通用户隐藏 admin 按钮** — `docker-compose.yml` `tg-web` command 从 `cp -rf templates 目录复制`(嵌套 bug,Flask 读旧版)改成 `templates/*.html` 文件级 glob,以后 templates / README / release_notes 改动 update.sh 后自动生效不用 docker exec 手动同步;`web.py::dashboard_page` 传 `is_admin` 给 template,`dashboard.html` 加 `IS_ADMIN` 全局 JS 标志,管理员才看到「立刻深度诊断」/「一键修复」/「立刻重启监听器」按钮,普通成员看到「请联系管理员」文字提示。CLAUDE.md 硬规定 #8 长期修法落地
> 之前:v3.0.8(2026-04-25) — 🚀 **Sheet 写入治本 + 卡死一键自助** — 写入用 `values.append` 替代 `update + col_values read`(quota 用量砍半 + 客户改表单不会被覆盖) + 全局令牌桶 50 req/min + 驾驶舱「立刻深度诊断」modal + 「一键修复」按钮 + 「立刻重启监听器」按钮(整合 v3.0.7.1) + 设置页 `SHEETS_FLUSH_INTERVAL` / `SHEETS_RATE_LIMIT_PER_MIN`
> 之前:v3.0.7(2026-04-25) — 🔁 **OAuth 重新授权后 Sheets 自愈** — 闭合 v3.0.6 的诊断—修复链路。客户在驾驶舱点「去重新授权」走完 OAuth,**5-30 秒内 Sheets 自动恢复写入**,不用 SSH `docker restart`。`flush_pending` 加 `RefreshError` 自愈,`OAUTH_FAIL_MARKERS` 抽到 `oauth_helper.py` 单一来源(诊断卡片 + 自愈逻辑共用)。`SheetsWriter._write_lock` 改 RLock 防递归死锁
> 之前:v3.0.6(2026-04-24) — 🛠 **驾驶舱三件套运维自助化** — 后台日志面板 + Sheet 堵塞自动诊断 + REMIND_DELETE 文案 UI
> 之前:v3.0.5(2026-04-24) — 🗑 **删除消息预警对齐 stage2 审批体验** — @负责人 + 登记违规/取消按钮(数据驱动,没配 owner_tg_id 的账号保持老路径)
> 之前:v3.0.4(2026-04-24) — 📣 **两段式预警 @username 改走 TG 原生解析** — 修 inline mention 反垃圾不通知问题
> 之前:v3.0.3(2026-04-23) — 🩺 **update.sh 升级时自动 Caddy 体检 + 自愈** — 承接 v3.0.2,把故障检测从"客户自己跑诊断工具"升到"升级自动自愈"。只动本部门相关的那一个 Caddy 容器,保护客户 VPS 上其他项目不受影响。客户零操作
> 之前:v3.0.2(2026-04-23) — 🛠 **Caddy inode 自愈 + `scripts/caddy-doctor.sh` 自查工具** — 修 shared caddy 模式多部门 HTTPS 失败(docker file bind mount inode 断裂)
> 之前:v3.0.0(2026-04-23) — 🆕 **两段式未回复预警 + TG 装置伪装** — 30 分钟 @ 商务 / 40 分钟 @ 负责人 + 违规/取消按钮 + 员工回复事件驱动自动结案 + Telethon 真名解析。全部 feature flag 默认关(`TWO_STAGE_NO_REPLY_ENABLED=false`),老客户升级零感知

---

## 目录

- [核心功能](#核心功能)
- [⚡ 5 分钟一键部署](#-5-分钟一键部署)
- [🧾 部署前准备清单](#-部署前准备清单)
- [📋 Setup 精灵完整步骤](#-setup-精灵完整步骤)
- [🔒 HTTPS 说明](#-https-说明)
- [🏢 同一台 VPS 多部门](#-同一台-vps-多部门)
- [🛠 日常维护](#-日常维护)
- [❗ 常见故障排查](#-常见故障排查)
- [🧩 开发 / 代码改动](#-开发--代码改动)
- [📜 版本](#-版本)

---

## 核心功能

- 多账号 Telethon 私聊消息监听(仅私聊,不含群组/频道)
- 消息落盘 Google Sheets(每部门一表格,每账号一分页,3 列横排)
- 图片/视频/语音自动上传到部门自己的 Google Drive(OAuth,15GB 免费额度)
- 关键词预警 → TG 群(到期/续费/暂停/下架/上架/地址/打款/欠费/返点/返利/回扣)
- 工作时段内累计 30 分钟未回复预警,非工作时段自动跳过
- 删除消息检测 + Sheet 标红删除线(60s 巡检)
- 每日零点北京时间日报
- 外事号昵称 / 广告主昵称变更自动同步到 Sheets 表头
- 同一对话同一类预警每天只触发一次
- Web 后台 + 设置精灵,零命令行配置
- **一键 HTTPS**:自动 Caddy + Let's Encrypt + nip.io 域名
- **智能兼容 VPS 现有 Caddy**:自动识别并接入外部反代,不抢端口
- **驾驶舱总览** `/dashboard`:账号矩阵 / 实时告警流 / 今日数据 / 系统状态一屏看完 (v2.10)
- **一键热升级按钮**:web 后台点一下自动拉新版 + 重建 + 健康检查 + 失败回滚 (v2.10)
- **TG 绑定 + 忘记密码**:管理员私聊 bot 绑定 TG,忘记密码时 DM 发验证码改密 (v2.10.1)
- **中央台接入**:每部署自动生成 METRICS_TOKEN,`/api/v1/metrics` Bearer 上报数据 (v2.8)
- **自动版本通知**:每 6h 查 GitHub 新版,Dashboard 横幅 + TG Bot 推送 (v2.9)

---

## ⚡ 5 分钟一键部署

### SSH 登入你的 VPS 后,执行这条:

```bash
curl -fsSL https://raw.githubusercontent.com/yifan1119/tg-monitor-template/main/install.sh -o /root/install.sh && bash /root/install.sh demo
```

> v2.10.14 起 HTTPS 默认开启,不用再加 `--https` 旗标。

> 把 `demo` 换成你的**部门英文代号**(只能小写字母 + 数字 + `-` / `_`,不能中文)。
> 中文显示名(如「示例公司」)在安装完的 setup 页里填,不影响这里。

**脚本会自动做的事**:

- ✅ 检测并安装 git / Docker / Docker Compose(缺啥装啥)
- ✅ 从 5001 开始扫描选一个空闲端口
- ✅ 从 GitHub 拉模板到 `/root/tg-monitor-demo/`
- ✅ 生成骨架 `.env` 文件
- ✅ 启动 Docker 容器(`tg-web-demo` + `tg-monitor-demo`)
- ✅ **智能 HTTPS**:
  - 若 80/443 空闲 → 启自建 Caddy,申请 nip.io 证书
  - 若 80/443 被**现成 Caddy** 占用 → 自动接入反代(零打扰)
  - 若 80/443 被其他服务(nginx/apache)占 → 打印接管说明
- ✅ 配置 ufw 防火墙(若启用)
- ✅ 外网可达性自检
- ✅ 打印 setup 页 URL + OAuth 回调 URI(复制到 Google Cloud 的重定向 URI 就行)

### 其他用法

```bash
# 指定 web 端口(HTTPS 仍默认开启)
bash install.sh demo 5003

# 用自己的域名(需先把 DNS A 记录指到本机 IP)
bash install.sh demo --https monitor.abc.com

# 纯 HTTP (特殊场景,一般别用)
bash install.sh demo --no-https

# 不带参数:列出已部署部门
bash install.sh
```

---

## 🧾 部署前准备清单

**三件事并行准备**,按需求复杂度排序:

### 1. VPS(必须)

- Linux(Ubuntu/Debian/CentOS 皆可),1 核 1G 起
- root SSH 权限
- **云厂商控制台放行端口**:
  - `80, 443` — HTTPS 用(Let's Encrypt 验证 + 服务)
  - `5001-5099` — 仅当你不启用 HTTPS 时需要
- Hostinger / 阿里云 / AWS / DO / Vultr 都 OK

### 2. Telegram 资源

| 项目 | 取得方式 |
|------|----------|
| **API_ID + API_HASH** | [my.telegram.org](https://my.telegram.org) 登入 → API development tools 建一个 app |
| **Bot Token** | 私聊 [@BotFather](https://t.me/BotFather) → `/newbot` → 拿到 `xxx:xxx` 格式 Token |
| **预警群 Chat ID** | 新建一个 TG 群 → 把 Bot 和 [@userinfobot](https://t.me/userinfobot) 都拉进去 → @userinfobot 会自动发群 Id(以 `-100` 开头的负数)→ **Bot 必须设为群管理员**,否则收不到预警 |
| **要监听的外事号** | TG 手机号 + 两步验证密码(如有) — 装完后在后台填 |

### 3. Google Cloud 资源

> 同一个 Google 帐号下建一次,9 个部门可共用同一个 GCP 项目。

**步骤**:

1. 打开 [Google Cloud Console](https://console.cloud.google.com/) → 新建项目(如 `tg-monitor`)
2. **启用 2 个 API**(容易漏,漏了会「建 Spreadsheet 失败」):
   - [启用 Google Drive API](https://console.cloud.google.com/apis/library/drive.googleapis.com)
   - [启用 Google Sheets API](https://console.cloud.google.com/apis/library/sheets.googleapis.com)
3. [OAuth 同意屏幕](https://console.cloud.google.com/apis/credentials/consent) → 用户类型选「**外部**」→ 填应用名 → **测试用户**区:+ ADD USERS → 把要授权的 Gmail 加进去
4. [创建 OAuth 客户端 ID](https://console.cloud.google.com/apis/credentials):
   - 应用类型:**Web 应用**(不是「桌面」!)
   - **已获授权的重定向 URI**:填 `https://<你的VPS-IP>.nip.io/api/oauth/callback`
     (装完 install.sh 最后会打印这个 URI,复制粘贴就行)
   - 拿到 **Client ID** + **Client Secret**,设置精灵里会用

---

## 📋 Setup 精灵完整步骤

`install.sh` 跑完浏览器打开 `https://<IP>.nip.io/setup`,分 4 个区域填写,每块都有实时测试按钮:

### ① 部门信息
- **部门代号**:跟 install.sh 命令的一致(如 `demo`)
- **显示名称**:填中文(如 `示例公司`),登入页、预警消息、Sheet 标题都会用这个

### ② Telegram
- `API_ID` / `API_HASH`:从 my.telegram.org 拿
- `BOT_TOKEN`:BotFather 给的
- `ALERT_GROUP_ID`:@userinfobot 在群里报的那串负数
- 点「**▸ 测试 Bot 连线**」→ 绿勾 = Bot + 群 + 管理员都 OK

### ③ Google Drive + Sheets(OAuth 授权)
- 填 `OAuth Client ID` / `Client Secret`
- 点「**▸ 连接 Google Drive**」→ 跳 Google 授权页(Gmail 必须在测试用户白名单)→ 同意
- 自动跳回设置页,**之前填的字段不会丢**(v2.6.1 修复)
- 点「**▸ 自动建表格**」→ 自动在你 Drive 建好 Spreadsheet,`SHEET_ID` 自动填入
- 点「**▸ 测试 Sheet 访问**」→ 绿勾

### ④ 业务参数(有默认值,可跳过)
- 关键词列表 / 未回复阈值 / 工作时段 / 巡检间隔等

---

最后点底部「**▸ 保存并启动**」→ 自动重启 tg-monitor 容器,几秒后跳登入页。
默认登入密码:`tg@monitor2026`(登入后进「设置 → 账户」改)。

---

### 设置完成后:加外事号

后台 **账号管理** → **+ 添加账号**:

1. 填国际格式手机号(如 `+8613912345678`)
2. TG 官方 app 收到验证码 → 填进来
3. 若开两步验证,再填 TG 登入密码
4. 完成,后台显示账号在线

一个部门可加任意多个号,每号自动占一个 Sheets 分页。

---

## 🔒 HTTPS 说明

OAuth 要求回调 URI 必须 HTTPS(或 localhost),VPS 上部署必须开 HTTPS。

### 三种场景 install.sh 都处理好了

| 场景 | install.sh 行为 |
|------|-----------------|
| **新干净 VPS**,80/443 空闲 | 启自建 Caddy,申请 `<IP>.nip.io` 证书,一条命令到底 |
| **VPS 上已有现成 Caddy**(做别的项目用) | 自动检测 → 不启自建 Caddy → 把 `tg-web` 接入现成 Caddy 网络 → 自动追加 Caddyfile site block + reload |
| **VPS 上有 nginx / apache / 系统原生 caddy** | 打印说明让你手动反代到 `http://localhost:<WEB_PORT>`,不会乱动你现有服务 |

### nip.io 是什么?

一个「把 IP 当域名用」的公开 DNS 服务,零配置:

- `<VPS_IP>.nip.io` → 自动解析回 `<VPS_IP>`(例:`203.0.113.5.nip.io` 解析回 `203.0.113.5`)
- Caddy 能用它申请 Let's Encrypt 证书,完全合法
- 免费、无需买域名,证书 90 天自动续

### 想用自己的域名?

安装时带域名参数:
```bash
bash install.sh demo --https monitor.abc.com
```

前提:把 `monitor.abc.com` 的 DNS A 记录先指到 VPS IP(TTL 低一点,立刻生效)。

---

## 🏢 同一台 VPS 多部门

完全隔离,互不影响:

- 目录:`/root/tg-monitor-<dept>/`
- 端口:从 5001 起自动扫描
- 容器:`tg-web-<dept>` / `tg-monitor-<dept>`
- Compose project:`tg-<dept>`
- HTTPS:每部门一个 nip.io 域名 + 独立证书
- 共享:同一个 GCP 项目、同一个 OAuth 同意屏幕都可以

直接多跑几次 install.sh 就行:

```bash
bash install.sh demo    # 5001
bash install.sh dept2   # 5002
bash install.sh dept3   # 5003
```

---

## 🛠 日常维护

```bash
cd /root/tg-monitor-demo

# 查日志
docker compose -p tg-demo logs -f web           # Web 后台
docker compose -p tg-demo logs -f tg-monitor    # 监听 + 定时任务

# 重启
docker compose -p tg-demo restart

# 停止
docker compose -p tg-demo down

# 拉最新代码 + 重启(不重建镜像)
git pull && docker compose -p tg-demo restart

# 拉最新代码 + 重建镜像(改了依赖再用)
git pull && docker compose -p tg-demo up -d --build

# 查该部门全部容器
docker ps | grep tg-.*-demo
```

### 升级(推荐用 update.sh — 带回滚保护)

```bash
cd /root/tg-monitor-demo
./update.sh
```

`update.sh` 会自动:
1. 把当前版本 sha 写入 `.last_commit`(回滚用)
2. 检测到本地修改的 tracked 文件 → 自动 `git stash` 保护(不会无声丢失)
3. `git fetch + git reset --hard origin/main`
4. `docker compose up -d --build` 重建镜像
5. 健康检查 60 秒,**失败自动回退到升级前版本**(含 stash 还原)

### 升级失败 / 想回退

```bash
# 一键回到上一版(读 .last_commit)
bash rollback.sh

# 回到指定 commit
bash rollback.sh <sha>

# 列出最近 10 个 commit 让你选
bash rollback.sh --list
```

`rollback.sh` 行为:
- 二次确认后再动手(交互终端)
- 先把当前 sha 也存进 `.last_commit`,**再跑一次 rollback.sh 可以前进回原本版本**
- 检测到本地修改自动 stash
- `git reset --hard <目标 sha>` + 重建容器 + 健康检查

### 备份(重要)

这 3 个东西丢了会很痛:

```bash
# 备份 session 文件(TG 登入态)+ 数据库 + .env
tar czf backup-demo-$(date +%F).tar.gz \
    /root/tg-monitor-demo/sessions \
    /root/tg-monitor-demo/data \
    /root/tg-monitor-demo/.env
```

Session 丢了要重新登外事号(收验证码);数据库丢了去重记录和历史指针丢失(不影响已写到 Sheets 的数据)。

### 卸载某个部门

```bash
cd /root/tg-monitor-demo
docker compose -p tg-demo down -v
cd /
rm -rf /root/tg-monitor-demo
```

---

## ❗ 常见故障排查

| 症状 | 原因 / 修复 |
|------|-------------|
| Setup 页「建 Spreadsheet 失败」/ `SERVICE_DISABLED` | Drive 或 Sheets API 没启用。去 [Drive API](https://console.cloud.google.com/apis/library/drive.googleapis.com) + [Sheets API](https://console.cloud.google.com/apis/library/sheets.googleapis.com) 点「启用」,等 10 秒再试 |
| OAuth 跳 Google 后 `access_denied` | 登入的 Gmail 不在「测试用户」白名单。去 [OAuth 同意屏幕](https://console.cloud.google.com/apis/credentials/consent) → 下方测试用户区 + ADD USERS |
| OAuth 跳回来字段被清空 | v2.6.1 已修。`git pull && restart web` 拉最新代码 |
| `redirect_uri_mismatch` | Google Cloud 的重定向 URI 跟当前访问 URL 不一致。`http` vs `https`、端口、域名必须完全对得上 |
| 设置精灵打开显示白页 | 镜像构建失败。`docker compose -p tg-demo logs web` 看真实报错 |
| HTTPS 打不开 / `ERR_CONNECTION_TIMED_OUT` | 云厂商防火墙没开 443。去控制台开 TCP 443 入站 |
| Bot 在群里收不到预警 | Bot 没设为群管理员。进群 → 群设置 → 管理员 → 添加管理员 → 选 Bot |
| 外事号登入失败 / `AUTH_KEY_UNREGISTERED` | session 文件坏了。后台删除该账号 → 重新添加 |
| 日志刷 `FLOOD_WAIT_X` | TG 短时间请求过多限流。等 X 秒自动恢复,不是 bug |
| 关键词匹配不中繁体中文 | 代码默认简体,`.env` 加 `KEYWORDS=到期,续费,暂停,下架,上架,地址,打款,欠费,返点,返利,回扣,續費,暫停,欠費,返點,回扣` |

排查不了贴**前 50 行日志**到 [GitHub Issue](https://github.com/yifan1119/tg-monitor-template/issues)。

---

## 🧩 开发 / 代码改动

### 目录结构

```
.
├── main.py                 # 主入口(TG 监听 + 定时任务)
├── bot.py                  # aiogram bot 处理器(预警推送 + 命令)
├── listener.py             # Telethon 多账号监听
├── tasks.py                # 定时任务(巡检 / 未回复 / 日报 / 昵称同步)
├── sheets.py               # Google Sheets 写入
├── media_uploader.py       # Drive 媒体上传 + 保留期清理
├── database.py             # SQLite 操作
├── web.py                  # Flask 后台(setup 精灵 + 登入外事号)
├── config.py               # 配置加载
├── oauth_helper.py         # Google OAuth 授权 + Drive/Sheets 建立
├── templates.py            # 预警消息模板
├── install.sh              # 一键安装(默认 HTTPS + 自动清孤儿容器)
├── enable_https.sh         # 独立启 HTTPS / 外部 Caddy 接入
├── update.sh               # 拉新代码 + 重启
├── docker-compose.yml
├── Dockerfile
├── Caddyfile               # 自建 Caddy 配置
└── templates/              # Web 后台 HTML (setup/login/dashboard/…)
```

### 修改后重新部署

代码以 volume 挂载进容器(`./repo:/app/repo:ro` + 启动时同步),**改完直接 restart 即可**,不用重建镜像:

```bash
# 改了 .py / templates/
docker compose -p tg-demo restart

# 改了 requirements.txt / Dockerfile(要重建镜像)
docker compose -p tg-demo up -d --build
```

### 修改工作时段

改 `config.py` 的 `WORK_SCHEDULE`(默认周一~五 11-13 / 15-19 / 20-23,周六 11-13 / 15-20,周日休),重启生效。

### 修改关键词

setup 精灵有「业务参数」区直接改,或编辑 `.env` 的 `KEYWORDS=...` 后重启。

---

## 🔐 安全说明

- `.env` / `google_oauth_token.json` / `sessions/` 都在 `.gitignore`,不会进 Git
- setup 精灵完成前 `/api/test-*` 不需登录(方便自检),完成后必须登录
- 防暴力破解:IP 10 分钟 5 次失败 → 锁 15 分钟(内存,重启清零)
- 支持 `X-Forwarded-For` 解析真实 IP(兼容 Cloudflare / tunnel)
- 多用户 + RBAC:首个账号为管理员,管理员可加普通账号;普通账号只能看、不能改配置
- Bot 连线测试只调 `getMe` + `getChatMember`,不发测试消息
- Sheets 连线测试幂等(读 A1 再写回原值)

---

## 📜 版本

- **v3.1** (2026-04-29) — 当前稳定版 📋 **(Sheet 后台扫描 + 客户删旧消息自动回填空位)**
  - [NEW] **`database.py` migration V6**(ADR-0027)— `peers` 加 `next_sheet_row INTEGER DEFAULT NULL` + `next_sheet_row_resynced_at TEXT DEFAULT NULL`,`idx_peers_next_row` 索引;7 个 helpers(get/set/bump/invalidate/get_all_with_col_group/get_max_resynced_at/get_all_accounts)
  - [NEW] **`sheets.py` 双轨写入**:`write_messages` 决策 update vs append fallback;`_write_messages_via_update` 命中 `peers.next_sheet_row`,响应 `updatedRange` 校验 row mismatch → invalidate 防御;`_write_messages_via_append` 抽 v3.0.8 老路径作 fallback;`_post_write_finalize` 共用 mark_written + 删除标红 backfill
  - [NEW] **`sheets.py resync_peer_positions`**:每 worksheet 1 次 `ws.get_all_values()` 整张拉,本地 `_scan_first_empty(values, col_start)` 找首空行更新 DB,持 `_write_lock` 跟 flush 串行
  - [NEW] **`tasks.py _sheet_position_resync_loop`**:启动等 30s,每 N 分钟 `asyncio.to_thread` 跑;每 5s 检查 `data/.sheet_resync_request` 文件触发 on-demand
  - [NEW] **`web.py /api/sheets/resync-now`**(@admin_required)— 写文件标志触发跨容器 IPC
  - [NEW] **`config.py` 3 新配置**:`SHEET_RESYNC_INTERVAL_MINUTES=15` / `SHEET_RESYNC_ENABLED=true` / `SHEET_RESYNC_VERIFY_BEFORE_WRITE=false`(强保护开关)
  - [NEW] **`dashboard_api.sheets_health` 4 新字段**:`resync_enabled / resync_interval_min / last_resync / last_resync_human`(跨容器走 `db.get_max_resynced_at()`)
  - [NEW] **驾驶舱 UI** Sheet 健康卡多一行「行号扫描:N 分钟前 (每 15 分钟) [立刻重扫]」(admin-only 按钮);设置页加扫描间隔 + 启用开关
  - [SAFETY] **0 数据迁移 0 重登 0 强制行为变化** — 老 peer `next_sheet_row=NULL` 自动走 append fallback,首次 resync 完成切 update;feature flag 关掉等价 v3.0.9
  - [SAFETY] **race 防御 3 层**:updatedRange row mismatch → invalidate / `SHEET_RESYNC_VERIFY_BEFORE_WRITE` acell 验空 / fallback append 兜底零覆盖
  - [QUOTA] **写 quota 不变**(1 API/peer 跟 v3.0.8 持平),读 quota 极省(15 min × 1 ws = 0.07 reads/min,大客户 100 ws 也仅 6.7 reads/min,远低 300/min 上限)
  - 升级:`cd /root/tg-monitor-<dept> && ./update.sh`

- **v3.0.9** (2026-04-29) 📊 **(中央台数据接口扩展 — 0 客户可见 UI 改动)**
  - [NEW] **`dashboard_api.accounts_matrix()` SELECT 扩字段**(ADR-0026)— 加 `tg_id / business_tg_id / owner_tg_id / remind_30min_text / remind_40min_text`,中央台拿得到 stage1/stage2 @对象 + 提醒模板
  - [NEW] **`dashboard_api.alerts_recent()` SELECT 扩字段** — 加 `status / stage / keyword / reviewed_at / sheet_written / claimed_at / last_write_error + account_id/peer_id/msg_id`,中央台能识别 violation_logged + 看 stage2 升级 + 关键词命中 + Sheet 写入对账
  - [NEW] **4 个新 `/api/v1/*` endpoint** — `violations`(违规登记明细)/ `alerts`(通用查 + 分页)/ `peers`(全监控聊天)/ `messages`(消息明细,强制 account_id+peer_id 必填防整表扫)
  - [NEW] **`_v1_check_token()` helper** 抽出 — 4 个新 endpoint 沿用 `_ensure_metrics_token()` Bearer / `?token=` 双路径鉴权,跟现有 `/api/v1/metrics` 同一套
  - [SAFETY] **0 新表 0 新字段 0 数据迁移** — 纯只读扩 SELECT + 加路由,不动 listener/sheets/bot/sessions,200+ TG 账号不重登
  - [SAFETY] **`messages_filtered` 强制 `account_id+peer_id` 必填** — 防整表扫拖死 SQLite
  - [SAFETY] **status / type / stage 白名单校验** — 即使走参数化 SQL 多一层防御
  - [SAFETY] **limit 硬上限**(alerts/messages 1000 / peers 5000)+ `_clamp_int()` 防滥用
  - 升级:`cd /root/tg-monitor-<dept> && ./update.sh`

- **v3.0.8.3** (2026-04-25) 🔧 **(修「立刻重启监听器」404 找不到容器)**
  - [FIX] **`web.py /api/restart` 改用 `_start_tg_monitor()` 复用 fallback**(ADR-0025)— 老 endpoint 直接 `client.containers.get('tg-monitor-' + COMPANY_NAME)` 找不到就 throw 404,现在 fallback 到本机任意 `tg-monitor-*`(`_start_tg_monitor` 早就有此逻辑,但 `/api/restart` 没复用)
  - [FIX] **`dashboard_api._diagnose_sheets_stuck` 加同样 fallback** — 部署遗留 COMPANY_NAME 错配的部门 Sheet 诊断也能用
  - [SCOPE] 不动设置页 COMPANY_NAME 锁定逻辑 — 部署 lifecycle 重新设计留 v3.1+
  - [FUTURE] session_health get_me() false positive 重试逻辑留 v3.0.9
  - 升级:`cd /root/tg-monitor-<dept> && ./update.sh`

- **v3.0.8.2** (2026-04-25) 🔧 **(升级提示去 SSH 包装 + 复制 fallback + 深度诊断永远可见)**
  - [FIX] **`upgrader.build_upgrade_cmd` 去掉 `ssh root@<IP>` wrap**(ADR-0024)— `.env` 配 `VPS_PUBLIC_IP` 时不再生成 `ssh root@1.2.3.4 "cd ... && bash update.sh"`(客户没有 root 凭据 + 命令本来就要在 VPS 跑,wrap 误导)。直接给 `cd ... && bash update.sh`
  - [FIX] **3 个 templates 复制按钮加 `copyTextFallback` 三层兜底** — HTTPS `navigator.clipboard` → HTTP `execCommand('copy')` → 终极 `prompt()` 弹窗。修 v3.0.8.1 客户「点了没复制」反馈
  - [NEW] **驾驶舱「Sheet 写入诊断」永远可见入口** — 日志面板上方新增 `{% if is_admin %}` 区块,管理员随时点「立刻深度诊断」查未写明细 / 孤儿消息 / col_group 缺失 + 一键修(复用 v3.0.8 modal,无新代码)
  - [UI] 升级 modal 文案 "📋 复制 SSH 命令" → "📋 复制升级命令";底部说明改 "涉及镜像重建的版本要在服务器跑命令(给你复制好,SSH 上去贴一下就行)"
  - 升级:`cd /root/tg-monitor-<dept> && ./update.sh`

- **v3.0.8.1** (2026-04-25) 🔧 **(docker cp 漏同步根治 + 普通用户隐藏 admin 按钮)**
  - [FIX] **docker-compose.yml `tg-web` command 改 `cp -rf templates 目录复制` → `templates/*.html` 文件级 glob**(ADR-0023)— 修 v3.0.8 客户「升级了看不到深度诊断按钮」的根因(嵌套 cp 让 Flask 读不到新版),CLAUDE.md 硬规定 #8 长期修法落地
  - [FIX] **`tg-web` + `tg-monitor` command 都加 `cp README.md` + `cp release_notes.json`** — 升级后 `_app_version_string` / update_checker 推送都用最新文案,不再吃镜像旧版
  - [UX] **`web.py::dashboard_page` 传 `is_admin` 给 template + `dashboard.html` 加 `IS_ADMIN` 全局 JS 标志** — 普通成员账号不再显示「立刻深度诊断」/「一键修复」/「立刻重启监听器」三个按钮(避免点了 403),改显示「请联系管理员重启监听器」文字提示
  - [SAFE] 后端 `/api/restart` `@login_required` **不变**(保留账号管理页历史按钮兼容);`/api/diag/sheets-stuck-detail` + `/api/diag/sheets-fix-stuck` `@admin_required` 维持 v3.0.8 收紧
  - 升级:`cd /root/tg-monitor-<dept> && ./update.sh`(必经 `docker compose up -d --build` 让新 command 生效)

- **v3.0.8** (2026-04-25) 🚀 **(Sheet 写入治本 + 卡死一键自助)**
  - [ARCH] **`write_messages` 改用 `values.append`** 替代 `update + col_values read`(ADR-0022)— 每个 peer 从 2 次 API call 砍到 1 次。客户在表里手动改/插/删行**不会被覆盖**(append 自动跟随当前末尾)
  - [ARCH] **全局令牌桶限流** — `_rate_limit` 加 60 秒滑动窗口最多 N 次 API call,默认 50,可配 `SHEETS_RATE_LIMIT_PER_MIN`(5-60)。Google 配额 60/min/user 不能超
  - [DIAG] **驾驶舱「立刻深度诊断」按钮 + modal** — 后台跑 SQL 列出孤儿消息(peer FK 失效)/ `col_group=NULL` peer / 缺 sheet_tab 的账号明细。新 `/api/diag/sheets-stuck-detail` GET endpoint
  - [FIX] **驾驶舱「一键修复」按钮** — 检测可修复项时显示。`/api/diag/sheets-fix-stuck` POST(`@admin_required`)action ∈ {orphan_messages / col_group_null / all},自动放弃孤儿消息(标 `sheet_written=1, last_write_error='ABANDONED_orphan_v308'`)/ 给 NULL peer 分配下一空闲列组
  - [UI] **驾驶舱「立刻重启监听器」按钮整合 v3.0.7.1** — 429 / 通用 warning 路径有,复用现有 `/api/restart` endpoint
  - [SETTING] **设置页加 2 个高级字段** — `SHEETS_FLUSH_INTERVAL`(1-600 秒)/ `SHEETS_RATE_LIMIT_PER_MIN`(5-60)。客户自助调流速,不用 SSH 改 .env
  - [DIAG] **诊断关键词收紧** — `worksheetnotfound` / `spreadsheet not found` / `permission_denied` 精确匹配,不再 false positive 把 Drive 上传 404 误判成 Sheet 不存在
  - 升级:`cd /root/tg-monitor-<dept> && ./update.sh`

- **v3.0.7** (2026-04-25) 🔁 **(OAuth 重新授权后 Sheets 自愈)**
  - [FIX] **闭合 v3.0.6 诊断—修复链路**(ADR-0021)— 客户在驾驶舱点「去重新授权」按钮走完 OAuth,Sheets 写入 **5-30 秒内自动恢复**,不用 SSH `docker restart`
  - [NEW] `SheetsWriter.reload_credentials()` — 重读 token + 重建 `gc` + 清空所有账号退避状态(否则新 token 拿到了但还卡在 600s 退避)。原子替换 — 中途失败保留旧 `self.gc` 不破坏现状
  - [NEW] `flush_pending` 加三层 OAuth 自愈 catch:`google.auth.exceptions.RefreshError`(主路径) + `gspread.APIError` 关键词兜底 + bare Exception 关键词兜底。**OAuth 检查在 429 检查之前**,避免 `"invalid_grant — quota project context lost"` 字样误吞退避
  - [REFACTOR] `OAUTH_FAIL_MARKERS` + `is_oauth_failure(text)` 抽到 `oauth_helper.py` 单一来源 — `sheets.py` 自愈 + `dashboard_api.py` 诊断卡片共用,避免 v3.0.6 的两份分歧 bug(`refreshError` camelCase 在 lowercased 文本里 dead-match,`oauth.*revoked` 是正则用作 substring,`401` 太宽松)
  - [SAFE] `_write_lock` 从 `threading.Lock()` 改 `threading.RLock()` — 防 `flush_pending` → `reload_credentials` 同线程递归死锁
  - [DESIGN] 不在 web.py callback 里直接 reset SheetsWriter — `tg-monitor` / `tg-web` 是两个独立容器(独立进程),跨进程没有共享内存。改走文件 IPC:`tg-web` 调 `save_token()` 写新 token 到 `data/google_oauth_token.json`(共享 docker volume),`tg-monitor` 下次 flush 自愈时读到。详见 ADR-0021 第 5 节
  - [UI] 设置页 OAuth 完成 banner 加 `(Google Sheets 写入将在 5-30 秒内自动恢复)` 文字提示
  - [OBSERV] 新增 `_oauth_reload_count` 计数器 + `[oauth_reload] gc 已重建 (累计 N 次)` 日志,可观察自愈次数
  - 升级:`cd /root/tg-monitor-<dept> && ./update.sh`(升级动作本身就会重启容器读新 token,如果客户现在卡在「授权了但还是不写」直接救活)

- **v3.0.6** (2026-04-24) 🛠 **(驾驶舱三件套运维自助化)**
  - [NEW] 后台日志查看面板(容器白名单防越权 + 注入防御正则)— 客户在浏览器看 tg-monitor / tg-web / tg-caddy log,不用 SSH
  - [NEW] Sheet 写入堵塞自动诊断 — 积压 ≥50 条 + ≥15 分钟没写入时扫 tg-monitor log 识别 OAuth 失效 / 429 / 无权限,显示红/黄条 + 修复按钮
  - [NEW] 补齐 v3.0.5 漏的 `REMIND_DELETE_TEXT` UI 输入框(系统设置「两段式未回复预警」区块加第三个 textarea)
  - [SAFE] 容器名白名单 `{tg-monitor-<dept>, tg-web-<dept>, tg-caddy-<dept>}` + `tg-caddy-*` 正则,严格拒绝 shell 注入字符
  - 详见 ADR-0020

- **v3.0.5** (2026-04-24) 🗑 **(删除消息预警对齐 stage2 审批体验)**
  - [NEW] 配了 `owner_tg_id` 的账号:删除消息预警 @负责人 + 登记违规/取消按钮(跟两段式 stage2 一致)
  - [NEW] 新增 `REMIND_DELETE_TEXT` 配置项(可自定义删除预警提示文案)
  - [COMPAT] 没配 `owner_tg_id` 的账号保持老通过/拒绝路径,完全向后兼容
  - 详见 ADR-0019

- **v3.0.4** (2026-04-24) 📣 **(两段式预警 @username 走 TG 原生解析)**
  - [FIX] `bot.py:_build_tg_mention` 优先级调整:`@username` 格式不再强转 inline mention,改用 TG 原生 `@text` 文本(bot inline mention 受反垃圾规则限制,没 /start 过 bot 的人收不到通知,改原生解析能稳稳触发)
  - [COMPAT] 纯数字 UID 仍走 Telethon 真名解析 + inline mention(没 username 时只能这样)
  - 详见 ADR-0018

- **v3.0.3** (2026-04-23) 🩺 **(update.sh 升级时自动 Caddy 体检 + 自愈)**
  - [NEW] `update.sh` 5.6 段:升级末尾自动对比本部门使用的 Caddy 的 host/容器 Caddyfile size,不一致自动 `docker restart` 修复
  - [SAFE] **只动本部门相关的那一个 Caddy**(own `tg-caddy-<company>` 或 shared Caddy 有本域名的那个)
  - [SAFE] 其他项目容器一律跳过 — 只看 `^tg-caddy-` 前缀,客户 VPS 上跑的其他 bot / 网站 / 私有服务一概不碰
  - [COMPAT] 单部门客户天然不会触发(没 shared caddy,inode 永远对得上)
  - 升级:`cd /root/tg-monitor-<dept> && ./update.sh`

- **v3.0.2** (2026-04-23) 🛠 **(Caddy inode 自愈 + caddy-doctor.sh)**
  - [FIX] **shared caddy 模式一台 VPS 部多部门 HTTPS 终于稳定**(ADR-0017)— 根因是 docker file bind mount 按 inode 绑定,
    `sed -i` / `cp` / vim 等原子替换会破坏 inode 链接 → 容器永远看老 Caddyfile → 新部门 site block 永不生效
  - [FIX] **`enable_https.sh` 追加后加 host/容器 Caddyfile size 对比**,不一致自动 `docker restart` 兜底重建 mount
  - [FIX] 所有 silent fail 改成 `exit 1`(找不到 Caddyfile bind mount / 追加后 grep 验证失败 / 证书 90 秒没签下来 → 明确告知 3 种原因)
  - [NEW] `scripts/caddy-doctor.sh` — 6 项检查:容器状态 / Caddyfile inode 同步 / 语法校验 / site block vs 运行容器(死站检测)/ 证书目录 / 最近 ACME 错误摘要
  - [SMALL] 生成的 site block 清除冗余 `header_up X-Forwarded-For` / `X-Forwarded-Proto`(Caddy 2 默认自动加,保留 `Host` 和 `X-Real-IP`)
  - [CLAUDE] CLAUDE.md 加硬规定 #9:Caddyfile 绝不用 sed -i / cp / vim,只能 `>>`
  - 升级:`cd /root/tg-monitor-<dept> && ./update.sh`

- **v3.0.1** (2026-04-23) 🛠 **(两段式按钮无条件渲染 + 驾驶舱版本号修正)**
  - [FIX] 账号管理页「配置两段式」按钮从 feature flag 条件渲染改成**数据驱动**(有 `s.id` 就显示,有配 `business_tg_id` 就走新路径)
  - [FIX] 驾驶舱版本号显示错版本(pack file 读不到 commit subject 导致 fallback 到老 reflog)— 多路径 fallback:tag → release_notes → loose object → reflog
  - [FIX] `update.sh` 加 `git fetch --tags` 避免本地 tag 落后触发错版本推送
  - [CLAUDE] 加硬规定 #8:每次部署 `docker exec` 同步 `.py` + `templates/` + `README.md` + `release_notes.json` 到容器,否则登入页/modal 显示老版本

- **v3.0.0** (2026-04-23) 🆕 **(两段式未回复预警 + TG 装置伪装)**
  - [NEW] **两段式未回复预警**(ADR-0015 + ADR-0016)— 30 分钟 @ 商务人员 + 40 分钟 @ 部门负责人 + 违规/取消按钮。
    员工在外事号 App 回复客户 → 事件驱动自动结案(outbound 钩子 + poll 兜底双路径)
  - [DB] **Migration V5**(ADR-0015)— `accounts` 加 `business_tg_id` / `owner_tg_id` / `remind_30min_text` / `remind_40min_text` 4 列;
    `alerts` 加 `stage` 列(0=老路径 / 1=stage1 / 2=stage2)。沿用 ADR-0005 决策:**保留 `type='no_reply'` 不变**,只加 stage 列 → 回滚兼容
  - [SAFE] **demo 错位 DB 兼容修复**(Codex C 方案)— `_run_migrations` 对 V5 关键列做存在性检查,
    防 demo 开发期 `user_version` 错位导致 migration 半崩(实际 schema 缺列仍能自愈补上)
  - [NEW] **Telethon 真名解析**(ADR-0016)— `_build_tg_mention` 调 `client.get_entity` 拿 numeric id + first_name + last_name,
    拼 HTML inline mention 显示「王小明」而不是蓝字 `@username`。解析失败降级到 `@xxx` 字符串兜底不崩
  - [NEW] **全域统一文案**(v2.10.26 测试期反馈)— `REMIND_30MIN_TEXT` / `REMIND_40MIN_TEXT` 一次改 `.env` 全部账号生效。
    账号级 `remind_30min_text` / `remind_40min_text` 仍可独立覆盖(给 VIP 特殊需求用)
  - [NEW] **独立预警群**(可选)— `UNREPLIED_ALERT_GROUP_ID` 填了就走独立群,否则 fallback 到 `ALERT_GROUP_ID` 老群
  - [NEW] **TG 装置伪装**(ADR-0016)— Telethon `TelegramClient` 显式传
    `device_model` / `system_version` / `app_version` 三字段,默认 `shencha` / `1.0` / `tglistener 1.0` 中性签名。
    客户可在 `.env` 改成 `TG Desktop` / `Windows 10` / `5.1.1` 模拟 TG Desktop(降低风控识别,不做 0 风险保证)
  - [NEW] **on_stage2_action callback** — `violation:{id}` / `cancel:{id}` 两按钮,权限沿用 `CALLBACK_AUTH_USER_IDS` 老白名单。
    `violation` → 写预警分表(6 列结构不变,`violation_logged` 写进「处理状态」列,不加末列);
    `cancel` → 仅 edit_text 删按钮
  - [COMPAT] `TWO_STAGE_NO_REPLY_ENABLED=false`(**默认**)→ `send_no_reply_alert` / `_write_alert_to_sheet` / 预警分表结构
    跟 v2.10.25 一字不差,老客户升级零感知
  - [COMPAT] 回滚安全:`bash rollback.sh` 回 v2.10.25 老代码仍能读 DB(新列保留无害),Sheet 结构不变
  - 升级:`cd /root/tg-monitor-<dept> && ./update.sh`

- **v2.10.25** (2026-04-23) 🔒 **(媒体存储可切 TG 档案群 — 规避 Google 账号冻结风险)**
  - [NEW] **`MEDIA_STORAGE_MODE` feature flag**(ADR-0014)— 三选一:
    - `drive`(**默认**,老客户无感)— 保留 v2.10.24 原逻辑,图片/文件上 Google Drive
    - `tg_archive` — Bot 把图片/文件/语音转发到独立 TG 群,Sheet 写 `=HYPERLINK(t.me/c/..., "图片 #N")` 超链接
    - `off` — 完全不处理媒体,Sheet 只显示 `[图片]` / `[文件]` 文字占位
  - [NEW] **设置页模式选择器** — 下拉切换三种模式,字段条件显示(选 tg_archive 隐藏 Drive 字段 + 显示档案群输入)
  - [NEW] **Bot `/chatid` 指令** — 在任何群 / 私聊发 `/chatid`,bot 按 chat 类型回复:supergroup ✅ 可作档案群 / 普通群附升级教程 / private 提示作审核白名单。替代第三方 `@RawDataBot` / `@userinfobot` 依赖
  - [NEW] **语音转发** — tg_archive 模式下 voice 也走 Bot `send_voice`(发送失败降级 `send_document` 保文件不丢)
  - [SAFE] **原子 `media_seq` 计数器** — 新 `account_seq` 表 + `INSERT OR IGNORE` + `UPDATE +1` + commit 原子分配,防三路协程并发重号(Codex P1 round1)
  - [SAFE] **档案群 ID supergroup 校验**(双层:web 保存前 + 运行时)— 必须 `-100xxx` 负数,否则保存挡住 + 运行时回落文字占位不会把客户图片 DM 到用户(Codex P1 round1)
  - [SAFE] **HTML escape bot 回复** — `/chatid` 回复用 `html.escape(title)` 防群名特殊字符让 HTML parser 崩(Codex P1 round2)
  - [FIX] **`main()` 预存 UnboundLocalError** — 函数内重复 `import os` 触发 SETUP_COMPLETE=false 分支崩,修掉冗余 import
  - [DB] **Migration 4** — messages 加 `media_seq` / `archive_msg_id` 列 + 新表 `account_seq`(幂等 `_safe_add_column` + `CREATE TABLE IF NOT EXISTS`)
  - [COMPAT] 默认 drive 模式 → 老客户 `./update.sh` 后行为 0 变化;切 tg_archive 完全自愿,回滚 `./rollback.sh` 安全(新列保留无害)
  - [TEST] 测试部署 9 项矩阵覆盖:图片/文件/语音转发成功 + 原子 seq 无跳号 + 错误 group ID 格式被挡 + HYPERLINK 深链可点
  - [REVIEW] Codex round1 + round2 双审,0 P0,4 个 P1 全修完
  - 升级:`cd /root/tg-monitor-<dept> && ./update.sh`

- **v2.10.24.6** (2026-04-22) 📝 **(文案白话化原则 — 客户看的是业务不是技术)**
  - [POLICY] **`release_notes.json` 文案必须白话**(ADR-0013)— 用户反馈「太复杂客户哪看得懂,我都看不懂」
  - 禁止出现:文件名 / 函数名 / 技术缩写(regex、config、SQL、DB、API)/ 代码细节 / 配额数字
  - 必须用:具象业务名词(表格 / 预警群 / 账号 / 弹窗)+ 白话问题描述(卡住 / 丢了 / 看不到)+ 效果描述(自动补 / 不再卡 / 零丢失)
  - v2.10.24 累计说明 + v2.10.24.5 / .6 key 都按新原则重写
  - 历史独立 key(v2.10.24.1 ~ .4)保留原版 — 反正已升到 v2.10.24.4+ 的客户看新 key,历史独立 key 读不到
  - **纯文案改动**,无代码改动
  - 升级:`cd /root/tg-monitor-<dept> && ./update.sh`

- **v2.10.24.5** (2026-04-22) 📝 **(文案补丁 — 旧客户也能看到正确升级说明)**
  - [FIX] **`release_notes.json` 的 `v2.10.24` key 改成累计说明**(ADR-0012)— 承接 v2.10.24.4 修客户端 regex 之后,
    还没升级的旧客户跑旧 regex,看弹窗仍会把 `v2.10.24.*` 截成 `v2.10.24` → 拿到旧的「容器冲突修补」说明
  - 改 `v2.10.24` key 的 value 为 v2.10.24 + .1/.2/.3/.4 的四版累计说明:
    - 旧客户(三段 regex):`v2.10.24.*` → `v2.10.24` key → **累计说明**(含四个 hotfix)
    - 新客户(贪婪 regex):`v2.10.24.5` 精确匹配 → 本 patch 独立说明
  - **纯 JSON 文案改动**,无代码 / 无业务逻辑变化 / 不影响已登录账号
  - 历史真相保留在 ADR-0007 / commit `d087c9d` / README 历史条目 — `release_notes.json` 只是 UI 文案
  - 升级:`cd /root/tg-monitor-<dept> && ./update.sh`

- **v2.10.24.4** (2026-04-22) 🩹 **(展示层修复 — 四段版本号升级说明)**
  - [FIX] **`update_checker` 版本号 regex 支持四段**(ADR-0011)— 原 regex `v\d+\.\d+\.\d+` 只匹配三段,
    `v2.10.24.3: ...` 会被截成 `v2.10.24` → 后台升级弹窗显示 v2.10.24 的「容器冲突修补」而不是 v2.10.24.3 的真实说明
  - 改成 `v\d+(?:\.\d+)+` 贪婪匹配任意段,未来出 5 段也能接住
  - **纯展示层修复**,不动业务逻辑 / 不改数据库 / 不影响已登录账号
  - 升级:`cd /root/tg-monitor-<dept> && ./update.sh`

- **v2.10.24.3** (2026-04-22) 🛡️ **(承接 v2.10.24.2 — 预警分页零丢失保障)**
  - [NEW] **预警分页整行缺失自动 writeback**(ADR-0010)— 修 bot.py 写预警分页只有 3 retry × 2s(共 6 秒)容错的老 bug:
    429 > 6 秒 / 网络短暂抖动 / worksheet 短暂不可达 → 整行预警数据永久缺失 → 客户反馈「不能丢失啊」
  - DB `alerts` 表加 `sheet_written` 标记位(类比 `messages.sheet_written` + `_sheets_flush_loop` 成熟模式),
    `keyword` 栏位独立存(修 message_text 存 `[kw] text` 混存问题)
  - 新增 `_alert_writeback_loop` 每 60 秒扫 `sheet_written=0` 的预警 → 补写到对应分页 → 成功 mark=1 → **无限重试**
  - `send_keyword_alert` 顺序反转 — 先 `insert_alert` 拿 id 再写 Sheet,写失败由 loop 接力
  - 配额 — 每轮最多 50 条,间隔 1.5s ~ 75 秒 API 消耗;碰 429 整轮截断,loop 间隔天然退避
  - 幂等 — 历史 alerts(升级前)一刀标 1 不追补,只保障升级后零丢失
  - [NEW] 配置 `ALERT_WRITEBACK_DISABLED=false`(默认开) / `ALERT_WRITEBACK_INTERVAL_SEC=60`(默认 60 秒)
  - 升级:`cd /root/tg-monitor-<dept> && ./update.sh`

- **v2.10.24.2** (2026-04-22) 🧹 **(承接 v2.10.24.1 — 自动补填预警分页历史空白)**
  - [NEW] **预警分页历史空白自动回填**(ADR-0009)— v2.10.24.1 之前 sync_headers 被 429/sed 止血卡住时,
    客户在外事号分页 B2(商务人员)/ B3(所属公司)填了也同步不到 DB,导致后续关键词监听/未回复/删除预警
    三个分页 A/B 栏一大片空白(历史脏数据)
  - 启动时 `sheets.__init__` 立即调 `backfill_alert_history()` 补一次,`_alert_backfill_loop` 每小时巡检一次
  - 幂等 — 只填空栏,有值的不动,跑多少次都安全。DB 里也空的外事号会 log 清单,客户按清单补 B2/B3
  - 配额 — 每轮 ~7 次 API 调用,远低于 60/min 配额
  - [NEW] 配置 `BACKFILL_ALERT_HISTORY=true`(默认) / `BACKFILL_ALERT_INTERVAL_SEC=3600`(默认 1 小时)
  - 升级:`cd /root/tg-monitor-<dept> && ./update.sh`

- **v2.10.24.1** (2026-04-22) 🚨 **(账号多客户必升 — Sheets 读配额保护)**
  - [FIX] **`sync_headers` 节流**(ADR-0008)— 原来每 60 秒对每个账号读 2 次 Sheets(`A2:B3` + `row 6`),
    150 账号 × 2 = 300 reads/min,打爆 Google 读配额(60/min/user)。连锁导致消息实时写入也 429。
    改成独立节流 `SYNC_HEADERS_INTERVAL_SEC=600`(10 分钟),默认读频降到 30 reads/min
  - [FIX] **`_peer_name_consistency_loop` 间隔独立化**(ADR-0008)— 原 docstring 写「每 10 分钟」
    但代码用 `PATROL_INTERVAL`(60s),每账号 1 读也能打爆配额。独立配置 `PEER_NAME_CONSISTENCY_INTERVAL_SEC=600`,
    顺手修 docstring-代码不一致 bug
  - [NEW] **紧急开关** `SYNC_HEADERS_DISABLED` / `PEER_NAME_CONSISTENCY_DISABLED`,遇到配额问题可一键关
  - 客户反馈:某 150+ 账号客户 2026-04-22 15:07 线上撞配额爆(sed 止血 16:25 立即生效,sed 见 ADR-0008)
  - 副作用:B2/B3 / 第 6 行手改后同步到 DB 延迟从 60s → 最多 600s(业务影响极小)
  - 升级:`cd /root/tg-monitor-<dept> && ./update.sh`(sed 止血改动会被 update.sh 自动 stash + reset,无需手动回滚)

- **v2.10.24** (2026-04-21) — **(升级流程修补,推荐升级避免卡住)**
  - [FIX] **`update.sh` orphan cleanup 放宽**(ADR-0007)— 以前只清 `compose
    project label` 对不上的同名容器,label 匹配但容器异常(上次升级中断残留)
    会跳过不清 → `docker compose up --build` 撞 `container name "/tg-xxx"
    is already in use` 升级失败。现在**无条件清同名容器**(跟 install.sh v2.10.20+
    对齐)
  - [FIX] **容器缺失检测** — 客户手动 `docker rm` 清过容器后跑 `./update.sh`
    被「当前已是最新版」误导退出,容器一直没重建监控服务长时间宕机。现在
    检测 `tg-monitor-<部门>` / `tg-web-<部门>` 是否存在,缺失则跳过 git pull
    但继续重建
  - [SAFE] **不碰 tg-caddy-<部门> 容器** — Caddy 是 profile 服务,v2.10.22
    末端的 HTTPS 保护块依赖容器「存在」才能检测 + 拉起,清了反而破坏 HTTPS
    自恢复机制。orphan cleanup 只清 monitor + web,caddy 交给保护块处理
  - 升级:`cd /root/tg-monitor-<dept> && ./update.sh`

- **v2.10.23** (2026-04-21) — **(推荐所有部署立即升级)**
  - [FIX] **Sheets 写入按账号分桶** — 以前 `flush_pending` 全局 LIMIT 500,
    任一账号撞 429 / 出错整批中断 → 下一轮又卡在同一批老消息 → 客户反馈「DB
    有消息但表格空白」根因。改成每账号独立桶(100 条/轮/账号),单账号失败 try/except
    隔离,429 per-account 指数退避(5s → 10s → 20s → ... → 600s),不卡全局
  - [FIX] **冻结账号 sweep 真 RPC 探测** — `_check_single_session` 加 `get_me()`
    调用,捕获 `UserDeactivatedBanError` / `UserDeactivatedError` 等 Telethon 异常。
    以前只用 `is_user_authorized()` 判活,冻结账号 session key 仍有效直接误判
    healthy → UI 一直显绿色 ONLINE
  - [FIX] **预警发送失败当天不再重试** — `has_alert_today` 改成只认真送达
    (`bot_message_id IS NOT NULL` 或 `status='silenced'`)。以前发送失败也会
    占去重记录导致整天不再重试;`ALERT_XXX_ENABLED=False` 时插入的记录也会
    错误静默整天
  - [FIX] **删除消息时序 bug** — 消息收到后立刻被删(sheet_row 还没写),
    `mark_deleted_in_sheet` 查到 `sheet_row=0` 直接 return → 永远不会标红。
    新增 `messages.delete_mark_pending` 列,删除时 sheet_written=0 则标 pending,
    `write_messages` 写完 sheet_row 后检查 pending → 立刻补标红删除线
  - [FIX] **Callback 原子抢占** — 新 `claim_alert_for_review(alert_id, new_status)`
    用 `UPDATE WHERE status='pending'` + `rowcount=1` 判断抢到权,避免两个
    群成员同时点按钮重复写预警分表
  - [FIX] **Callback 身份校验(可选)** — 新增 `CALLBACK_AUTH_USER_IDS` .env 字段
    (数字 TG ID 逗号分隔),配了就只允许白名单里的人点审核按钮。空值跳过
    校验(老部署无感升级)
  - [FIX] **`upsert_account` 不再覆盖业务字段** — 以前 ON CONFLICT 会把客户在
    Web 后台填的 `company/operator` 被 listener 启动登录时的空值清空。改成
    ON CONFLICT 只更新 TG 身份字段(`name/username/tg_id`)。新增
    `update_account_business(id, company=..., operator=...)` 给 Web 后台用
  - [FIX] **`sync_headers` 单账号异常隔离**(ADR-0006)— 以前 accounts 表里某个
    账号分页出错(被手改名/删分页/单账号撞 429),`ws.get("A2:B3")` 抛出导致
    **后面的账号永远不被同步**,客户在 B2/B3 填了「渠道人员/中心·部门」但 DB
    没读到 → 预警推送显示空白。改成每账号 try/except 隔离,单账号失败只跳过
    该账号,其他账号照常同步(跟 ADR-0001 `flush_pending` 同逻辑)
  - [FIX] **SQLite 并发锁** — 加 `PRAGMA busy_timeout=5000`(5 秒等锁),避免
    多协程/多进程并发访问时直接抛 `database is locked`
  - [NEW] **DB migration 框架** — 加 `PRAGMA user_version` + `_safe_add_column`
    helper,未来加列走显式 migration 幂等机制,不再靠 CREATE TABLE IF NOT EXISTS
    的 side effect
  - [NEW] **Sheets 写入积压告警** — 新增 `_sheets_backlog_loop`(5 分钟一次),
    超 `SHEETS_BACKLOG_ALERT_THRESHOLD`(默认 500)条 sheet_written=0 且老
    10 分钟 → 推预警群告警(1 小时冷却不刷屏),提示去查 Google 配额/OAuth
  - 升级:`cd /root/tg-monitor-<dept> && ./update.sh`(全部 bug fix,行为向后兼容,
    DB migration 幂等,一键 `bash rollback.sh` 回 v2.10.22)

- **v2.10.22** (2026-04-20)
  - [FIX] `update.sh` 末端新增 HTTPS 保护块 — 如果看到 `tg-caddy-<部门>` 容器
    存在但不在 `running` 状态(例如之前有人跑过 `docker compose down`),
    自动跑 `docker compose -p tg-<部门> --profile https up -d caddy` 把它拉回来
  - 只对"挂了的 Caddy"动手,正在跑的不碰 — 避免引入不必要的 recreate
    导致 HTTPS 瞬断
  - 拉起失败只打 warning,不会让 `set -e` 把整个升级标成失败触发回滚
    (主服务已经过健康检查,Caddy 救不回来是独立问题)
  - 纯 HTTP 部署(没装过 Caddy)看不到这段执行,零影响
  - 升级:`cd /root/tg-monitor-<dept> && ./update.sh`

- **v2.10.21** (2026-04-20)
  - [FIX] `web.py` 改成防御性 `import telethon.errors` — 用 `getattr(telethon.errors, ...)`
    + `_MissingTgError` 占位 — 以前 v2.10.19 直接 `from telethon.errors import
    PasswordHashInvalidError, PhoneCodeInvalidError, ...`,如果客户 VPS 上
    Telethon 版本偏旧(例如 1.36.0 没有某些 error class),升级后 web 容器
    启动时 `ImportError` 直接挂,登录页打不开
  - `_humanize_tg_error()` 的 `isinstance` 检查对缺失的 error class 永远走
    `False` 分支 → fallback 到"剥掉 (caused by XxxRequest) 尾巴"的 str 兜底
  - 升级:`cd /root/tg-monitor-<dept> && ./update.sh`

- **v2.10.20** (2026-04-20)
  - [FIX] `update.sh` 加上同名容器清理(跟 install.sh 逻辑对齐)— 以前只有
    install.sh 清 orphan,update.sh 没清,遇到老 compose 文件生的容器或 label
    丢失的容器,`docker compose up --build` 会报 `container name "/tg-xxx-<dept>"
    is already in use`,升级卡住
  - [FIX] install.sh 原本只清"compose project 标签不是 tg-<部门>"的容器
    (v2.10.14 的谨慎判断),现在放宽到"名字对就清",因为
    `tg-(monitor|web|caddy)-<部门>` 这三个名字本来就是当前部门独占的,
    不怕误伤
  - 升级:`cd /root/tg-monitor-<dept> && ./update.sh`

- **v2.10.19** (2026-04-20)
  - [FIX] 登录 TG 账号(发验证码 / 验证码 / 两步验证密码)的错误提示
    从 Telethon 原始英文(例如 `The password (and thus its hash value) you
    entered is invalid (caused by CheckPasswordRequest)`)改成白话
    (例如「两步验证密码错误,请重新输入(区分大小写)」)
  - 新增 `_humanize_tg_error(e)`:覆盖 `PasswordHashInvalidError` /
    `PhoneCodeInvalidError` / `PhoneCodeExpiredError` / `PhoneNumberInvalidError` /
    `PhoneNumberBannedError` / `FloodWaitError`(带 N 秒/分/小时自动换算)等;
    兜底剥掉 `(caused by XxxRequest)` 技术尾巴
  - 升级:`cd /root/tg-monitor-<dept> && ./update.sh`

- **v2.10.18** (2026-04-20)
  - [FIX] `upgrader._run_upgrade` 覆盖完 tarball 后,立刻把 `.git/refs/heads/main`
    (和 `packed-refs` 里的 main 行)写成 `latest_sha` — 以前 `PRESERVE` 把
    `.git` 保留不覆盖,导致 `update_checker._read_local_sha` 永远读到 install.sh
    当时 git clone 下来的老 sha,一键软升级成功后弹窗阴魂不散
  - [FIX] 升级完成后立刻调一次 `update_checker.check_once()` 刷新
    `update_status.json`,前端下次查状态就能看到 `has_update=False`
  - 升级:`cd /root/tg-monitor-<dept> && ./update.sh`(update.sh 走 git pull,
    会自动把 refs 更新到 v2.10.18)

- **v2.10.17** (2026-04-20)
  - [FIX] `ensure_account_tabs` 除了补建不存在的分页,也会扫所有已存在的
    账号分页,如果是阉割版(frozenRowCount<6)就原地升级成完整模板 —
    补 row 4-6 样式 / 对话槽 header / 冻结 6 行 / 列宽 / 10 槽斑马纹,
    row 7+ 消息数据完全保留。存量客户 v2.10.16 之前的阉割版分页不用手动
    删,升级后 60 秒自愈巡检自动升
  - 新增方法:`SheetsWriter.upgrade_minimal_tab(ws)` +
    `_fetch_frozen_rows_map` / `_fetch_banded_ranges`(fetch metadata 一次
    批量判断,避免逐个分页 API 往返)
  - 升级:`cd /root/tg-monitor-<dept> && ./update.sh`

- **v2.10.16** (2026-04-20)
  - [FIX] sweep `ensure_account_tabs` 补建的分页改用完整模板(青色 header +
    对话槽 + 冻结 6 行 + 10 槽斑马纹),跟登录时 `_create_sheet_tab` 一致 —
    以前 sweep 只跑 `_init_sheet_header` 产出 3 行阉割版,导致 OAuth 失效
    恢复后漏建的分页结构不全,写消息时找不到对话槽位置
  - [重构] 把两路建分页逻辑合并到 `SheetsWriter.create_account_tab_full`,
    web.py 和 sheets.py 都调它,杜绝以后再 drift
  - 升级:`cd /root/tg-monitor-<dept> && ./update.sh`

- **v2.10.15** (2026-04-20)
  - [FIX] tasks.py `_patrol_loop` 每 60 秒也调一次 `ensure_account_tabs` —
    修「批量登录多个账号时少数分页没建出来」:`_create_sheet_tab` 登录时
    静默失败(Sheets 429 / 瞬时网络 / OAuth 缓存)+ tg-monitor 启动 sweep
    只跑一次的双重漏洞。现在漏建的分页 60 秒内巡检会补上,不用重启
  - 升级:`cd /root/tg-monitor-<dept> && ./update.sh`

- **v2.10.14** (2026-04-20)
  - [FIX] `/api/test-sheets` 验证通过时立刻把 SHEET_ID 写进 `.env` — 解决
    用户只点「测试 Sheet 访问」但没点「保存并启动」导致 SHEET_ID 丢失、
    tg-monitor 启动报 `RuntimeError: SHEET_ID 为空`、三个预警分页永远没被建
  - [FIX] `install.sh` 启动前自动扫 `tg-(monitor|web|caddy)-<dept>` 同名
    孤儿容器(项目 label 对不上当前部门的才清),避免 "container name
    already in use" 卡住重装
  - [UX] `install.sh` HTTPS 默认开启 — 改旗标为 `--no-https` 显式关闭
    (之前要加 `--https`,新人经常漏)
  - 升级:`cd /root/tg-monitor-<dept> && ./update.sh`

- **v2.10.13** (2026-04-18)
  - [FIX] `enable_https.sh` 同 VPS 多部门 HTTPS 共存冲突修复:以前所有部门默认
    domain 都是 `<IP>.nip.io`,Caddy site block 重复 → 后装的覆盖先装的 → 先装
    的部门 HTTPS 入口失效.现在新部门默认 `<company>.<IP>.nip.io` 子域,各自独立
    证书,多部门共存互不干扰
  - [兼容] 老部门 `.env` 已有 `PUBLIC_DOMAIN` → 继续沿用,Google OAuth redirect
    URI 不用改
  - 升级:`cd /root/tg-monitor-<dept> && ./update.sh`

- **v2.10.12** (2026-04-18)
  - [FIX] `/api/sheets/auto-create` 加全局锁 — 修「同一部门 Drive 里看到两个
    sheet,一个空一个有模板」的真凶:用户第一次点没反应再点一次,并发两个请求都
    read_env 看到 SHEET_ID 空 → 都调 Drive API 建新 sheet(Drive API eventual
    consistency 查同名查不到刚建的)→ 最后 write_env 只保留一个,另一个孤儿
  - 修复方式:`@_synchronized(_auto_create_sheet_lock)` 串行化 check-then-create
  - 升级:`cd /root/tg-monitor-<dept> && ./update.sh`

- **v2.10.11** (2026-04-18)
  - [FIX] 新部门装完 OAuth + 点「自动建表格」后 Sheet 一片空白(只有 Google 默认的
    工作表1)问题:以前预警分页只在 tg-monitor 启动时建,而 tg-monitor 没 session
    文件会直接 return 不实例化 SheetsWriter → 还没登入账号的新部门 sheet 永远空
    现在 web.py api_auto_create_sheet 建完立刻 new SheetsWriter() 触发
    ensure_alert_tabs,用户看 Sheet 就直接有 3 个预警分页
  - [FIX] SHEET_ID 已存在时也 heal 一次(幂等调 SheetsWriter),修老部门空白 sheet
    的历史遗留 — 进设置页再点一次「自动建表格」就会补齐
  - 升级:`cd /root/tg-monitor-<dept> && ./update.sh`

- **v2.10.10** (2026-04-18)
  - [FIX] `sheets.get_or_create_sheet()` 修复名不副实:以前名字叫 get_or_create
    但 docstring 和实现都只 get 不 create,跟 web UI 步骤 03「登录成功后 Sheets
    会自动建好对应分页」直接矛盾 → 历史遗留账号永远没有分页,消息不写
    现在真的 auto-create(A2/A3 留空等商务填 B2/B3,第一条消息进来自动填对话槽)
  - [NEW] `SheetsClient.ensure_account_tabs()` 启动 sweep:扫 DB 里所有账号,
    Sheet 里没有对应分页的,启动时补建(解决 v2.10.10 前登录的老账号无分页问题)
  - [FIX] `web._create_sheet_tab()` 失败改打完整 stack trace(以前只 print 一行
    错误讯息,看不到原因)
  - 升级后:tg-monitor 重启 → ensure_account_tabs 扫一轮 → 缺失分页自动补齐
  - 升级:`cd /root/tg-monitor-<dept> && ./update.sh`

- **v2.10.9** (2026-04-18)
  - [UX] 驾驶舱 KPI「账号 X / Y」改以 session_status 为准:session healthy 即算连线 OK
    群里没人发言 (heartbeat warn / silent) 不再被判定为「不健康」下调分子
    副标改分四档 (X 吊销 · X 活跃 · X 等待首条 · X 慢 · X 静默)
  - [UX] snapshot system 新增字段:accounts_connected / accounts_revoked / accounts_active
    / accounts_slow / accounts_silent。老前端读 accounts_online/warn/dead 兼容不变
  - [UX] KPI 颜色:全部 connected → 绿 · 有 revoked → 红 · 部分未连 → 黄
  - 升级:`cd /root/tg-monitor-<dept> && ./update.sh`

- **v2.10.8** (2026-04-18)
  - [UX] 驾驶舱 KPI「账号 0/1 · 1 死」误报修复:刚登入 / 群内还没人发言的账号
    不再被判定为死账号,改为 waiting 状态 → 显示「● 等待首条消息」(绿)
    KPI 分母计入「正常」,副标注出等待数,收到首条消息自动切回 online
  - [UX] accounts_matrix.heartbeat_status 新增 waiting 枚举,前端对应 ● 等待首条消息
  - [UX] KPI 「X 死」只计 session_status==revoked 或 心跳 >4h,不再把「没心跳 + healthy」当死
  - 升级:cd /root/tg-monitor-<dept> && ./update.sh

- **v2.10.7** (2026-04-18)
  - [FIX] 验证码/两步密码登录成功后,`.session_states.json` 立刻标 healthy — UI 不再 stale
    - 根因:tg-monitor 的 `_session_health_loop` 要等 90s 首轮延迟 + 历史消息拉完(媒体多可能几分钟)
    - 之前用户重新登录后,账号管理页还显示「🔴 会话已吊销」很久,体验差
    - 现在 verify_code / verify_password 成功立刻写 state 文件 + 推「外事号恢复通知」到预警群
  - [NEW] `web._push_session_restored()` 绕开 aiogram(web 容器没 bot 实例),直接 HTTP POST
    `api.telegram.org/bot{token}/sendMessage` — 轻量可靠
  - [DOCS] web 登录 → UI 立刻变绿的完整链路:
    1. web.py `verify_code` 成功 → `_mark_session_healthy(phone)` 写 `.session_states.json`
    2. `_push_session_restored(phone, name)` 推恢复通知
    3. `_schedule_listener_restart()` 4 秒后重启 tg-monitor
    4. tg-monitor 重启后读新 session,`_session_health_loop` 首轮看到 healthy 不重复推
  - 升级:`cd /root/tg-monitor-<dept> && ./update.sh`

- **v2.10.6** (2026-04-18)
  - [FIX] 账号管理页(index.html)的 `● Online` badge 之前写死,session 被吊销后仍显示绿色
    - 改成读 `data/.session_states.json`,有 `revoked` 状态显示「🔴 会话已吊销」红 badge
    - 另加 `error`(检查异常)黄 badge 覆盖临时网络故障
    - 驾驶舱之前 v2.10.4 已经修过,这次补上账号管理页
  - [NEW] `web.get_sessions()` 携带 `session_status` 字段,模板可直接读
  - 升级:`cd /root/tg-monitor-<dept> && ./update.sh`

- **v2.10.5** (2026-04-18)
  - [FIX] `bot.send_session_alert()` 移除 `ALERTS_ENABLED` 主开关闸门
    - 根因:客户把业务告警主开关关了 → 结果连「外事号掉线」这种运维故障也不推
    - 架构划分:ALERTS_ENABLED 只管业务告警(关键词/未回复/删除),session 吊销是
      系统性故障(根本不工作了),必须永远推
    - 仍然只需 `bot + ALERT_GROUP_ID` 配好即生效
  - 升级:`cd /root/tg-monitor-<dept> && ./update.sh`

- **v2.10.4** (2026-04-18)
  - [NEW] TG 会话吊销监测 (client.is_user_authorized() 每 5 分钟巡检一次)
    - healthy → revoked 转场:TG 预警群推「外事号离线预警」+ 写 alerts 表(实时告警流显示)
    - revoked → healthy 转场:推「外事号恢复通知」
    - 状态持久化到 `data/.session_states.json`,容器重启不会重复告警
    - 首轮 90s 延后 + 基线模式(不炸群),error 状态不触发转场
  - [NEW] 驾驶舱账号卡:session 吊销时 pulse badge 覆盖为「🔴 会话已吊销」(优先级高于心跳)
  - [FIX] main.py 启动期全军覆没不再退出容器 → 改为等待模式(bot 继续运行,允许 /bind 指令)
    - 附带推送每个账号的 session_revoked 预警(客户一开始就能看到"你哪些号掉了")
  - 受 ALERTS_ENABLED 主开关控制(master off 时只写日志不推 TG)
  - 升级:`cd /root/tg-monitor-<dept> && ./update.sh`

- **v2.10.3** (2026-04-18)
  - [FIX] 登入页/设置页底部版本号不再硬编码(之前 login 显示 v2.6.12、setup 显示 v2.8.0
    都是老版本留的),改从 README 最新版 banner 动态读取(SSOT:改 README 即跟着变)
  - [NEW] `update_checker._notes_for()` 3 层 fallback:release_notes.json short_sha →
    version_tag → commit subject 自动生成白话标题 + emoji(未来 Ivan 不用维护
    release_notes.json,直接写正常 git commit 就行)
  - [NEW] `_auto_emoji()` 关键词 → emoji 映射:fix→🔧 / sec→🔒 / feat→🆕 / docs→📖 /
    refactor→🧹 / perf→⚡ / ui|dashboard→🎛️ / 兜底→📦
  - 升级:`cd /root/tg-monitor-<dept> && ./update.sh`

- **v2.10.2** (2026-04-18)
  - [FIX] bot.py `send_update_notice()` `latest_short` 未定义 → NameError 炸栈
    (改用 `state.get` 读取;py3.11 不支持嵌套同引号 f-string)
  - [SEC] `auth_reset.consume_reset_code()` 加尝试次数上限 (错 5 次锁死该 pending)
    防暴力猜码,锁死时写 audit_log `reset_code_locked`,用户需重新申请
  - [SEC] Flask `secret_key` 移除硬编码,改为每部署随机生成持久化到 `data/.flask_secret`
    老部署首次启动自动生成,已有登入 session 会失效需重登一次
  - 升级:`cd /root/tg-monitor-<dept> && ./update.sh`

- **v2.10.1** (2026-04-18)
  - [NEW] TG 绑定 + 忘记密码:管理员在账号管理页绑定 TG (私聊 bot 发 `/bind XXXXXX`),
    登入页点「忘记密码」→ bot DM 6 位验证码 → 输入验证码改新密码
  - [FIX] `update.sh` 先比对远端 SHA 才 stash,已是最新直接退出不动本地
    — 避免 OLD==NEW 时 stash pop 覆盖新版文件(v2.10.0 实测踩过)
  - [NEW] `update.sh` 自动补 v2.10 新字段:`INSTALL_DIR` / `VPS_PUBLIC_IP` 老部署升级无感
  - [UX] 驾驶舱移除无功能的「预警开关」pill(账号管理页已有,避免重复误导)
  - 升级:`cd /root/tg-monitor-<dept> && ./update.sh`

- **v2.10.0** (2026-04-18)
  - [NEW] 驾驶舱 `/dashboard`:账号矩阵 + 实时告警流 + 今日 KPI + 系统状态(5s 轮询)
  - [NEW] 一键热升级按钮:web 后台「检查更新」→ 有新版弹 modal → 点升级自动跑
    update.sh(`INSTALL_DIR` + 用户 IP 白名单校验,60s 健康检查,失败自动回滚)
  - [NEW] `.env` 新增 `INSTALL_DIR` + `VPS_PUBLIC_IP`(upgrader 用)
  - 升级:`cd /root/tg-monitor-<dept> && ./update.sh`

- **v2.9.2** (2026-04-17)
  - [FIX] 移除本地实验泄漏到 main 的「驾驶舱」链接(v2.10 正式上后重新加回)

- **v2.9.1** (2026-04-17)
  - [UX] 版本更新通知文案改白话(去掉 commit sha / release tag 等技术词)
  - [调参] GitHub 检查频率 1h → 6h(避免打扰 + 留 rate limit)

- **v2.9.0** (2026-04-17)
  - [NEW] 自动版本更新检测:update_checker 每 6 小时查 GitHub main branch
  - [NEW] 发现新版 → Dashboard 顶部横幅 + TG Bot 群组推送(含 commit message + 升级命令)
  - [NEW] `/api/update/check` 手动触发检查

- **v2.8.1** (2026-04-17)
  - [FIX] `METRICS_TOKEN` 兜底迁移:update.sh 没补到的老部署,web.py 启动时再补一次

- **v2.8.0** (2026-04-17)
  - [NEW] 中央台接入:`/api/v1/metrics` 用 Bearer token 鉴权,每部署独立 `METRICS_TOKEN`
  - [NEW] 设置页「中央台接入」板块:一键复制 token,可重置
  - [NEW] update.sh 自动为老部署生成 `METRICS_TOKEN`

- **v2.7.0** (2026-04-17)
  - [NEW] `update.sh` 加回滚保护:升级失败 60s 健康检查不过 → 自动 `git reset` 到升级前 sha + 重建容器
  - [NEW] `rollback.sh`:手动一键回退上个版本(读 `.last_commit`)
  - [NEW] 本地修改自动 stash 保护,保留 `.env` / `data/` / `sessions/`

- **v2.6.12** (2026-04-16)
  - [UX] ⓘ 帮助提示文案改成客户白话(去掉 listener / hot-reload / SHEET_ID 等技术词)
  - [FIX] 删掉「Telethon session 异常」这条 — 写错了,session 死掉重启监听修不了,
    要重新走加号流程;改成 ※ 备注引导客户去重新加号
  - [UX] tooltip 宽度 340→380 容纳更长备注行
  - 升级:`cd /root/tg-monitor-<dept> && ./update.sh`

- **v2.6.11** (2026-04-16)
  - [FIX] ⓘ 帮助提示浮层下半部分还是被「添加账号」card 盖住 — 因为下方 card 的
    backdrop-filter 创建了独立 stacking context;给 #status-card 加 z-index:1000
    让整张状态卡(含 tooltip)永远在最上层
  - 升级:`cd /root/tg-monitor-<dept> && ./update.sh`

- **v2.6.10** (2026-04-16)
  - [FIX] ⓘ 帮助提示浮层被 status-card 的 overflow:hidden 切掉下半部分 →
    给 #status-card 单独放行 overflow:visible,完整显示
  - 升级:`cd /root/tg-monitor-<dept> && ./update.sh`

- **v2.6.9** (2026-04-16)
  - [UX] 「重启监听」按钮加 ⓘ 帮助提示 + 弱化样式 — 暗示客户「正常不用点」,
    悬停 ⓘ 看完整说明(5 种实际需要手动重启的场景)
  - 升级:`cd /root/tg-monitor-<dept> && ./update.sh`

- **v2.6.8** (2026-04-16)
  - [NEW] KEYWORDS / NO_REPLY_MINUTES / PEER_ROLE_LABEL / OPERATOR_LABEL / COMPANY_DISPLAY
    全部改成 hot-reload — 设置页改完保存秒级生效,不用等 tg-monitor 容器重启
  - [架构] listener._handle_message 进来时先 reload_if_env_changed,新消息进来即时 pickup .env 变更
  - 升级:`cd /root/tg-monitor-<dept> && ./update.sh`

- **v2.6.7** (2026-04-16)
  - [NEW] 删除消息预警改成「实时」— 注册 events.MessageDeleted handler,对方点
    「同时为对方删除」后秒级触发(原来 60s 巡检,延迟 1~10 分钟)
  - [架构] 60s 巡检保留作兜底:listener 启动前的删除 / 实时事件漏接都靠它补
  - [防重] 实时 handler 先 mark_deleted 再推送,巡检看到 deleted=1 自动跳过
  - 升级:`cd /root/tg-monitor-<dept> && ./update.sh`

- **v2.6.6** (2026-04-16)
  - [NEW] 三类预警拆成独立开关 — 关键词 / 未回复 / 删除消息 各自一个 hot-toggle
  - [UI] Dashboard 顶部改成三个 chip,任一类关掉都会显示橙色横幅说明
  - [UI] Settings 推送开关区改成三行独立勾选,顺带保留日报开关
  - [API] 新增 `/api/alerts/subswitch/toggle` (body: type=keyword|no_reply|delete, enabled);旧 `/api/alerts/toggle` 改成「一键全开/全关」
  - [兼容] `ALERTS_ENABLED` 仍作为 fallback,旧部署升级后默认行为不变
  - 升级:`cd /root/tg-monitor-<dept> && ./update.sh`

- **v2.6.5** (2026-04-16)
  - [FIX] 改 PEER_ROLE_LABEL 后,现有外事号分页内每个对话槽 row 6(B6/E6/H6/K6/...)的角色字样
    现在也会自动同步,补 v2.6.4 之前就有的同步缺口
  - 升级:`cd /root/tg-monitor-<dept> && ./update.sh`

- **v2.6.4** (2026-04-16)
  - [NEW] 操作人员称谓可配置(默认「商务人员」,改成「业务员/负责人/客服」等都行)
  - [NEW] 改 OPERATOR_LABEL 后,系统自动同步现有 Sheet 所有外事号分页 A2 + 3 个预警表表头 + TG 预警消息字样
  - 升级:`cd /root/tg-monitor-<dept> && ./update.sh`(默认值不变,旧部署无感升级)

- **v2.6.3** (2026-04-16)
  - [NEW] 添加/删除账号后自动重启监听(debounce 4 秒,批量加号只重启一次)
  - [UX] 删掉「点上方 [重启监听] 启动监控」操作步骤,自动化掉

- **v2.6.2** (2026-04-16)
  - [NEW] 预警推送总开关 — Dashboard 顶部一键开关,关闭时 TG 群静音但 Sheets + DB 照常记录
  - [NEW] 日报独立开关 — Settings 页可单独控制每日零点日报推送
  - [UX] 关闭推送期间橙色横幅持续提醒,避免忘开

- **v2.6.1** (2026-04-16)
  - [NEW] 外部 Caddy 自动检测 + 反代接入
  - [NEW] nip.io 用 `.` 格式(更直观,`<VPS_IP>.nip.io`)
  - [FIX] OAuth 跳转不再清空已填字段(sessionStorage 自动恢复)
  - [FIX] 识别 `SERVICE_DISABLED` 错误 → 前端直接提示启用哪个 API
  - [UX] Setup 教学补「启用 Drive + Sheets API」关键步骤
  - [UX] Chat ID 获取改 @userinfobot(一步到位)

- **v2.6** (2026-04-15) — 统一霓虹科技风 UI + 多用户 RBAC + 一键 HTTPS(基线版)
