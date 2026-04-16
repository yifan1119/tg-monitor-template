# TG Monitor — 多部门 TG 私聊监控模板

**Telegram 私聊监控系统**,专为业务审查/合规场景设计:监听外事号私聊、关键词预警、未回复提醒、删除消息溯源,全量落盘到 Google Sheets。一条命令装完 Docker + HTTPS + 后台,非技术同事也能部。

> 📌 **最新版**:v2.6.1(2026-04-16) — 外部 Caddy 自动兼容 / 表单防丢失 / OAuth 流程优化

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

---

## ⚡ 5 分钟一键部署

### SSH 登入你的 VPS 后,执行这条:

```bash
curl -fsSL https://raw.githubusercontent.com/yifan1119/tg-monitor-template/main/install.sh -o /root/install.sh && bash /root/install.sh yueda --https
```

> 把 `yueda` 换成你的**部门英文代号**(只能小写字母 + 数字 + `-` / `_`,不能中文)。
> 中文显示名(如「悦达」)在安装完的 setup 页里填,不影响这里。

**脚本会自动做的事**:

- ✅ 检测并安装 git / Docker / Docker Compose(缺啥装啥)
- ✅ 从 5001 开始扫描选一个空闲端口
- ✅ 从 GitHub 拉模板到 `/root/tg-monitor-yueda/`
- ✅ 生成骨架 `.env` 文件
- ✅ 启动 Docker 容器(`tg-web-yueda` + `tg-monitor-yueda`)
- ✅ **智能 HTTPS**:
  - 若 80/443 空闲 → 启自建 Caddy,申请 nip.io 证书
  - 若 80/443 被**现成 Caddy** 占用 → 自动接入反代(零打扰)
  - 若 80/443 被其他服务(nginx/apache)占 → 打印接管说明
- ✅ 配置 ufw 防火墙(若启用)
- ✅ 外网可达性自检
- ✅ 打印 setup 页 URL + OAuth 回调 URI(苏总会看到「这里填到 Google Cloud」)

### 其他用法

```bash
# 不启用 HTTPS(纯内部测试)
bash install.sh yueda

# 指定端口
bash install.sh yueda 5003 --https

# 用自己的域名(需先把 DNS A 记录指到本机 IP)
bash install.sh yueda --https monitor.abc.com

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
- **部门代号**:跟 install.sh 命令的一致(如 `yueda`)
- **显示名称**:填中文(如 `悦达`),登入页、预警消息、Sheet 标题都会用这个

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
bash install.sh yueda --https monitor.abc.com
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
bash install.sh yueda --https    # 5001
bash install.sh dingfeng --https  # 5002
bash install.sh lingyuan --https  # 5003
```

---

## 🛠 日常维护

```bash
cd /root/tg-monitor-yueda

# 查日志
docker compose -p tg-yueda logs -f web           # Web 后台
docker compose -p tg-yueda logs -f tg-monitor    # 监听 + 定时任务

# 重启
docker compose -p tg-yueda restart

# 停止
docker compose -p tg-yueda down

# 拉最新代码 + 重启(不重建镜像)
git pull && docker compose -p tg-yueda restart

# 拉最新代码 + 重建镜像(改了依赖再用)
git pull && docker compose -p tg-yueda up -d --build

# 查该部门全部容器
docker ps | grep tg-.*-yueda
```

### 备份(重要)

这 3 个东西丢了会很痛:

```bash
# 备份 session 文件(TG 登入态)+ 数据库 + .env
tar czf backup-yueda-$(date +%F).tar.gz \
    /root/tg-monitor-yueda/sessions \
    /root/tg-monitor-yueda/data \
    /root/tg-monitor-yueda/.env
```

Session 丢了要重新登外事号(收验证码);数据库丢了去重记录和历史指针丢失(不影响已写到 Sheets 的数据)。

### 卸载某个部门

```bash
cd /root/tg-monitor-yueda
docker compose -p tg-yueda down -v
cd /
rm -rf /root/tg-monitor-yueda
```

---

## ❗ 常见故障排查

| 症状 | 原因 / 修复 |
|------|-------------|
| Setup 页「建 Spreadsheet 失败」/ `SERVICE_DISABLED` | Drive 或 Sheets API 没启用。去 [Drive API](https://console.cloud.google.com/apis/library/drive.googleapis.com) + [Sheets API](https://console.cloud.google.com/apis/library/sheets.googleapis.com) 点「启用」,等 10 秒再试 |
| OAuth 跳 Google 后 `access_denied` | 登入的 Gmail 不在「测试用户」白名单。去 [OAuth 同意屏幕](https://console.cloud.google.com/apis/credentials/consent) → 下方测试用户区 + ADD USERS |
| OAuth 跳回来字段被清空 | v2.6.1 已修。`git pull && restart web` 拉最新代码 |
| `redirect_uri_mismatch` | Google Cloud 的重定向 URI 跟当前访问 URL 不一致。`http` vs `https`、端口、域名必须完全对得上 |
| 设置精灵打开显示白页 | 镜像构建失败。`docker compose -p tg-yueda logs web` 看真实报错 |
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
docker compose -p tg-yueda restart

# 改了 requirements.txt / Dockerfile(要重建镜像)
docker compose -p tg-yueda up -d --build
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

- **v2.6.1** (2026-04-16) — 当前稳定版
  - [NEW] 外部 Caddy 自动检测 + 反代接入
  - [NEW] nip.io 用 `.` 格式(更直观,`187.77.157.220.nip.io`)
  - [FIX] OAuth 跳转不再清空已填字段(sessionStorage 自动恢复)
  - [FIX] 识别 `SERVICE_DISABLED` 错误 → 前端直接提示启用哪个 API
  - [UX] Setup 教学补「启用 Drive + Sheets API」关键步骤
  - [UX] Chat ID 获取改 @userinfobot(一步到位)

- **v2.6** (2026-04-15) — 统一霓虹科技风 UI + 多用户 RBAC + 一键 HTTPS(基线版)

详细 roadmap(v2.7 ~ v3.0)见开发文档。

---

## 许可 / 联系

- Repo: [github.com/yifan1119/tg-monitor-template](https://github.com/yifan1119/tg-monitor-template)
- 客户接案项目,二次开发请联系 Ivan
