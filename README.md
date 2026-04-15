# TG Monitor — 多部门 TG 私聊监控模板

为悦达业务审查部开发的 Telegram 私聊监控系统,支持快速复制部署给其他部门。同一台 VPS 可并行跑多个部门,端口自动分配。

## 核心功能

- 多账号 Telethon 私聊消息监听(仅私聊,不含群组/频道)
- 消息记录到 Google Sheets(每部门一表格,每账号一分页,3 列横排)
- 图片/视频/语音自动上传到部门自己的 Google Drive(OAuth 授权,15GB 免费额度)
- 关键词预警 → TG 群(到期/续费/暂停/下架/上架/地址/打款/欠费/返点/返利/回扣)
- 工作时段内累计 30 分钟未回复预警(非工作时段自动跳过)
- 删除消息检测 + Sheet 标红删除线(60s 巡检)
- 每日零点北京时间日报
- 同一对话同一类预警每天只触发一次(按日期 + peer_id 去重)
- 外事号昵称 / 广告主昵称变更自动同步到 Sheets 表头
- 用户手动改 Sheets 分页名 → 巡检自动改回 TG 名字
- Web 后台 + 设置精灵,零命令行配置
- 安装时自动 HTTPS(Caddy + Let's Encrypt + nip.io)

## 技术栈

- Python 3.11 + Telethon + aiogram 3.x + gspread + Flask + SQLite
- Docker + Docker Compose v2
- Caddy(HTTPS 反代,按需启用)

## 一键安装

### 推荐:直接用 HTTPS(OAuth 授权必须)

```bash
# yueda 换成你的部门英文名(小写英文/数字/短横)
curl -fsSL https://raw.githubusercontent.com/yifan1119/tg-monitor-template/main/install.sh \
  | bash -s -- yueda --https
```

安装脚本会自动:

- 扫描可用端口(5001 起),避开已占用的
- 装 git / Docker / Docker Compose(缺啥装啥)
- 拉模板到 `/root/tg-monitor-yueda/`
- 生成骨架 `.env`
- 启动容器(project 名 `tg-yueda`,互不冲突)
- 启用 HTTPS → 自动申请 `<IP转换>.nip.io` 证书
- 探测云厂商防火墙,提示放行端口
- 打印 setup 页链接 + OAuth 回调 URI

### 其他用法

```bash
# 不启用 HTTPS(只内部测试)
curl -fsSL .../install.sh | bash -s -- yueda

# 指定端口
curl -fsSL .../install.sh | bash -s -- yueda 5003 --https

# 用自己的域名(需先把 DNS A 记录指到本机)
curl -fsSL .../install.sh | bash -s -- yueda --https monitor.abc.com
```

### 非技术客户部署教程

完整带图文教程见 `TG监控-完整部署教程.html`(适合零基础运营),涵盖:

1. 申请 Telegram API
2. 创建 Bot + 预警群
3. Google Cloud 项目 / Service Account / OAuth
4. 新建 Google 表格并授权
5. VPS 一键安装
6. 设置精灵操作(4 步)
7. 登录外事号
8. 三步验证部署成功
9. 常见故障排查

## 安装后:打开 setup 页完成配置

装完浏览器打开 `https://<your-ip>.nip.io/setup`,设置精灵 4 步:

| 步骤 | 填什么 | 从哪来 |
|---|---|---|
| 1. Telegram API | API_ID + API_HASH | [my.telegram.org](https://my.telegram.org) 申请 |
| 2. Google Drive OAuth | 上传 OAuth client JSON → 点授权 | Google Cloud → 凭据 → OAuth 客户端 ID |
| 3. Google Sheets | SHEET_ID + Service Account JSON | 新建 Sheet + Google Cloud → 服务账号 |
| 4. Telegram Bot | BOT_TOKEN + ALERT_GROUP_ID | [@BotFather](https://t.me/BotFather) + 把 Bot 设群管理员 |

每步都有实时验证(权限检查 / 写入测试 / Bot 管理员状态),绿勾全过 → 点「完成设置并启动」→ 自动跳登录页。

默认登录密码:`tg@monitor2026`(登录后请去设置改)。

## 加外事号

登录后台 → 账号管理 → + 添加账号 → 填手机号 → 填 TG app 里收到的验证码(如果开了两步验证还要填 TG 密码)→ 完成。

一个部门可加任意多个外事号,每个号自动分配一个 Sheets 分页。

## 日常维护

```bash
cd /root/tg-monitor-yueda

# 日志
docker compose -p tg-yueda logs -f web
docker compose -p tg-yueda logs -f tg-monitor

# 重启
docker compose -p tg-yueda restart

# 停止
docker compose -p tg-yueda down

# 拉最新代码 + 重建
git pull && docker compose -p tg-yueda up -d --build
```

## 代码改动后重新部署

代码用 `COPY . .` 进镜像,单纯 scp 不生效:

```bash
# 整个重建
docker compose -p tg-yueda up -d --build

# 临时热替换某个文件(不重建镜像)
docker cp listener.py tg-web-yueda:/app/
docker cp listener.py tg-monitor-yueda:/app/
docker compose -p tg-yueda restart
```

## 多部门同 VPS

同一台 VPS 部署多个部门完全 OK:

- 每部门一个独立目录 `/root/tg-monitor-<name>/`
- 端口自动从 5001 起扫描,每部门不同
- 容器名 `tg-web-<name>` / `tg-monitor-<name>`,project 名 `tg-<name>`,互不冲突
- Service Account JSON / OAuth JSON 可共用一份
- HTTPS 每部门自己申请 nip.io 证书

查看当前部署情况:

```bash
# 不带参数跑安装脚本会列出已部署部门
bash install.sh
```

## 工作时段(代码默认)

- 周一～周五:11:00-13:00 / 15:00-19:00 / 20:00-23:00
- 周六:11:00-13:00 / 15:00-20:00
- 周日:休息

如需修改,改 `config.py` 的 `WORK_SCHEDULE`。

## 关键词

默认(简体):`到期,续费,暂停,下架,上架,地址,打款,欠费,返点,返利,回扣`

通过设置页的「业务设置」或 `.env` 的 `KEYWORDS` 改。如果团队是繁中,记得加 `續費,暫停,欠費,返點,回扣`。

## 目录结构

```
.
├── main.py                  # 主入口(TG 监听 + 定时任务)
├── bot.py                   # aiogram bot 处理器(预警推送 + 命令)
├── listener.py              # Telethon 多账号监听
├── tasks.py                 # 定时任务(巡检/未回复/日报/昵称同步/媒体清理)
├── sheets.py                # Google Sheets 写入
├── media_uploader.py        # Drive 图片上传 + 保留期清理
├── database.py              # SQLite 操作
├── web.py                   # Flask web 后台(设置精灵 + 登入外事号)
├── config.py                # 配置加载
├── templates.py             # 预警模板
├── install.sh               # 一键安装(支持 --https)
├── enable_https.sh          # 独立启用 HTTPS(已被 install.sh 调用)
├── docker-compose.yml       # 服务: tg-monitor + web (+ caddy if HTTPS)
├── Dockerfile
├── requirements.txt
└── templates/               # web 后台 HTML
    ├── setup.html           # 首次设置精灵
    ├── login.html
    ├── dashboard.html
    └── ...
```

## 安全说明

- `.env` / `service-account.json` / `oauth_token.json` 都在 `.gitignore`,不会被提交
- 设置精灵完成前 `/api/test-*` 不需要登录(方便首次自检),完成后必须登录
- Bot 验证只调 `getMe` + `getChatMember`(不发测试消息,不骚扰群成员)
- Sheets 验证读 A1 → 写回同值,幂等无副作用

## 已知行为(非 bug)

- 列组 0 首次写入会报 addBanding warning — `_create_sheet_tab` 已建好 A-C 斑马纹,不影响数据
- DB peers 数 > 列组数:历史对话但近 N 天无消息的 peer `col_group=-1`,等新消息到来才 lazy 分配(避免空对话占位)
- nip.io 证书有效期 90 天 Caddy 自动续签,无需干预
