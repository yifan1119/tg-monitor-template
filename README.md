# TG Monitor — 多部门 TG 私聊监控模板

**Telegram 私聊监控系统**,专为业务审查/合规场景设计:监听外事号私聊、关键词预警、未回复提醒、删除消息溯源,全量落盘到 Google Sheets。一条命令装完 Docker + HTTPS + 后台,非技术同事也能部。

> 📌 **最新版**:v2.10.11(2026-04-18) — 新部门建表后立刻初始化预警分页(以前要等登入账号才建,没登入前 sheet 全白)

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
curl -fsSL https://raw.githubusercontent.com/yifan1119/tg-monitor-template/main/install.sh -o /root/install.sh && bash /root/install.sh demo --https
```

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
# 不启用 HTTPS(纯内部测试)
bash install.sh demo

# 指定端口
bash install.sh demo 5003 --https

# 用自己的域名(需先把 DNS A 记录指到本机 IP)
bash install.sh demo --https monitor.abc.com

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

- `187.77.157.220.nip.io` → 自动解析回 `187.77.157.220`
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
bash install.sh demo --https    # 5001
bash install.sh dept2 --https  # 5002
bash install.sh dept3 --https  # 5003
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
├── install.sh              # 一键安装(支持 --https)
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

- **v2.10.11** (2026-04-18) — 当前稳定版
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
  - [NEW] nip.io 用 `.` 格式(更直观,`187.77.157.220.nip.io`)
  - [FIX] OAuth 跳转不再清空已填字段(sessionStorage 自动恢复)
  - [FIX] 识别 `SERVICE_DISABLED` 错误 → 前端直接提示启用哪个 API
  - [UX] Setup 教学补「启用 Drive + Sheets API」关键步骤
  - [UX] Chat ID 获取改 @userinfobot(一步到位)

- **v2.6** (2026-04-15) — 统一霓虹科技风 UI + 多用户 RBAC + 一键 HTTPS(基线版)
