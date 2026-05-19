"""推送消息模板 — 按商务提供的格式"""
import html as _html
import config


def _swap_company(c):
    """v3.1.6: account.company 存储格式是「公司-中心」(下拉拼接),
    但文案字段标签是「中心/部门」, 用户期望显示「中心-部门」顺序 → 反转。
    输入「鼎丰公司-商务中心」→ 输出「商务中心-鼎丰公司」。
    无 '-' 或拆不出来 → 原样返回。"""
    if not c or "-" not in c:
        return c or ""
    parts = c.rsplit("-", 1)
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        return f"{parts[1].strip()}-{parts[0].strip()}"
    return c


def _alert_title_label(company):
    """v3.2.0: 预警标题用 account.company(账号归属),让标题跟正文「中心/部门」一致。
    跨公司账号(账号归属跟 dept .env COMPANY_DISPLAY 不同)预警标题也跟着账号归属变。
    account.company 空 → fallback dept config.COMPANY_DISPLAY(兼容老 dept 没配归属)。
    """
    swapped = _swap_company(company)
    return swapped or getattr(config, "COMPANY_DISPLAY", "") or ""


def no_reply_alert(company, operator, account_name, peer_name, message_text):
    return (
        f"【信息未回复预警{_alert_title_label(company)}】\n\n"
        f"中心/部门：{_swap_company(company)}\n"
        f"{config.OPERATOR_LABEL}：{operator}\n"
        f"外事号：{account_name}\n"
        f"{config.PEER_ROLE_LABEL}：{peer_name}\n"
        f"未回复信息：{message_text}"
    )


def no_reply_alert_stage1(company, operator, account_name, peer_name,
                          message_text, business_mention="", custom_text=""):
    """v3.0.0 批次 B: stage1 未回复预警(30 分钟触发,@ 商务人员,无按钮)。

    business_mention: `_build_tg_mention` 生成的 HTML mention 片段;空串代表账号没配
                      business_tg_id,模板自动省略尾行 @ 退化成纯提醒(仍发预警不报错)。
    custom_text:      REMIND_30MIN_TEXT 全域文案;空则用内置默认「请尽快回复」。

    HTML parse_mode 必须:纯文字字段统一 html.escape 防止 `& < >` 让 TG HTML parser 炸。
    business_mention 已由 `_build_tg_mention` 内部处理过 escape,这里不再二次动。
    """
    e = _html.escape
    base = (
        f"【信息未回复预警{_alert_title_label(company)}】\n\n"
        f"中心/部门：{e(_swap_company(company))}\n"
        f"{config.OPERATOR_LABEL}：{e(operator)}\n"
        f"外事号：{e(account_name)}\n"
        f"{config.PEER_ROLE_LABEL}：{e(peer_name)}\n"
        f"未回复信息：{e(message_text)}"
    )
    tail_text = e(custom_text.strip()) if custom_text else "请尽快回复"
    if business_mention:
        return f"{base}\n\n{business_mention} {tail_text}"
    # 账号没填 business_tg_id → 只写提醒文案,不带 @
    return f"{base}\n\n{tail_text}"


def no_reply_alert_stage2(company, operator, account_name, peer_name,
                          message_text, owner_mention="", custom_text=""):
    """v3.0.0 批次 B: stage2 未回复升级(stage1 后 NO_REPLY_STAGE2_AFTER_MIN 分钟仍未回,
    @ 负责人,带「登记违规/取消」按钮)。
    owner_mention 空则退化不带 @。纯文字字段统一 html.escape。
    """
    e = _html.escape
    base = (
        f"【信息未回复升级{_alert_title_label(company)}】\n\n"
        f"中心/部门：{e(_swap_company(company))}\n"
        f"{config.OPERATOR_LABEL}：{e(operator)}\n"
        f"外事号：{e(account_name)}\n"
        f"{config.PEER_ROLE_LABEL}：{e(peer_name)}\n"
        f"未回复信息：{e(message_text)}"
    )
    tail_text = e(custom_text.strip()) if custom_text else "已超过 40 分钟未回复,请处理"
    if owner_mention:
        return f"{base}\n\n{owner_mention} {tail_text}"
    return f"{base}\n\n{tail_text}"


def delete_alert(company, operator, account_name, peer_name, message_text="",
                 owner_mention="", custom_text=""):
    """v3.0.5 客户反馈: 跟 stage2 预警一致的审批体验 —
    - 账号配了 owner_tg_id → 追加 @负责人 尾行 + 自定义文案,模板自动 html.escape
    - 账号没配 → 退化成老格式,保持完全向后兼容

    owner_mention 非空时才走 HTML 路径(调用方负责 parse_mode='HTML')。
    """
    if owner_mention:
        e = _html.escape
        text = (
            f"【信息删除预警{_alert_title_label(company)}】\n\n"
            f"中心/部门：{e(_swap_company(company))}\n"
            f"{config.OPERATOR_LABEL}：{e(operator)}\n"
            f"外事号：{e(account_name)}\n"
            f"{config.PEER_ROLE_LABEL}：{e(peer_name)}"
        )
        if message_text:
            text += f"\n已删除信息：{e(message_text)}"
        tail_text = e(custom_text.strip()) if custom_text else "请核实并做审批"
        return f"{text}\n\n{owner_mention} {tail_text}"

    # 老路径 — 保持完全不变,兼容没配 owner_tg_id 的账号
    text = (
        f"【信息删除预警{_alert_title_label(company)}】\n\n"
        f"中心/部门：{_swap_company(company)}\n"
        f"{config.OPERATOR_LABEL}：{operator}\n"
        f"外事号：{account_name}\n"
        f"{config.PEER_ROLE_LABEL}：{peer_name}"
    )
    if message_text:
        text += f"\n已删除信息：{message_text}"
    return text


def keyword_alert(company, operator, account_name, peer_name, keyword, message_text):
    return (
        f"【关键词监听{_alert_title_label(company)}】\n\n"
        f"中心/部门：{_swap_company(company)}\n"
        f"{config.OPERATOR_LABEL}：{operator}\n"
        f"外事号：{account_name}\n"
        f"{config.PEER_ROLE_LABEL}：{peer_name}\n"
        f"关键词：{keyword}\n"
        f"消息内容：{message_text}"
    )


def daily_report(report_date, record_time, chat_count,
                 no_reply_count, delete_count, keyword_count,
                 no_reply_detail=None, delete_detail=None):
    """
    no_reply_detail / delete_detail: 可选 dict {"approved": n, "pending": n, "rejected": n}
    有值时日报会附上审批分桶 (已通过 / 待审 / 已拒)
    """
    def _fmt(total, detail):
        if not detail:
            return f"{total}"
        return f"{total}  (已通过 {detail.get('approved', 0)} / 待审 {detail.get('pending', 0)} / 已拒 {detail.get('rejected', 0)})"

    return (
        f"【外事号监控总结】\n\n"
        f"统计日期：{report_date}\n"
        f"记录时间：{record_time}\n"
        f"监控聊天总数：{chat_count}\n"
        f"未回复数量：{_fmt(no_reply_count, no_reply_detail)}\n"
        f"信息删除数量：{_fmt(delete_count, delete_detail)}\n"
        f"关键词监听数量：{keyword_count}"
    )

def _vps_tag(host_ip="", company_display=""):
    """v3.0.17: 拼「部门 (IP)」标签 — 监察员一眼能看到出事的是哪台 VPS。
    company_display 留空 → 走 config.COMPANY_DISPLAY;host_ip 留空 → 不显示括号。

    Codex P0 fix: bot.send_session_alert 用 parse_mode='HTML' 推,部门名 / IP 含
    & < > 会让 TG 解析报错 → 整条预警发不出去。这里 html.escape 兜底。"""
    import html as _html
    cd = company_display or getattr(config, "COMPANY_DISPLAY", "") or getattr(config, "COMPANY_NAME", "")
    cd_safe = _html.escape(str(cd))
    if host_ip:
        return f"{cd_safe} ({_html.escape(str(host_ip))})"
    return cd_safe


def _safe_name(account_name, phone):
    """v3.0.17 Codex P0 fix: account_name 和 phone 含 < & > 会让 TG HTML 解析报错。
    inspector_mention 由 bot._build_tg_mention 自己 escape,这里只 escape 用户填的字段。"""
    import html as _html
    name = _html.escape(str(account_name)) if account_name else "—"
    ph = _html.escape(str(phone)) if phone else ""
    return name, ph


def session_revoked_alert(phone, account_name, inspector_mention="", host_ip="", company_display=""):
    """v3.0.17(was v2.10.4): TG 会话被吊销(用户在 TG 官方 App 点「终止其他会话」会触发)。
    新增:加 @ 监察员 + 部门/VPS IP 标签,出事第一时间到人。

    所有用户填的字段都做 HTML escape — bot.send_session_alert 用 parse_mode='HTML' 推。"""
    tag = _vps_tag(host_ip, company_display)
    name_safe, phone_safe = _safe_name(account_name, phone)
    head = f"【外事号离线预警 · {tag}】\n\n"
    body = (
        f"外事号:{name_safe} ({phone_safe})\n"
        f"状态:❌ 登录会话已失效\n"
    )
    if inspector_mention:
        # inspector_mention 已经在 _build_tg_mention 内部 escape 过 — 这里直接拼
        body += f"监察员:{inspector_mention}\n"
    body += (
        "\n"
        "可能原因:\n"
        "  • 你在 TG 官方 App「设置→设备」点了「终止其他会话」\n"
        "  • 账号被 TG 风控封禁或限制登录\n"
        "  • Session 文件损坏\n\n"
        "处理方式:\n"
        "  1) 打开 Web 后台 → 账号管理 → 重新登录该账号\n"
        "  2) 输入验证码完成登录即可恢复监听\n\n"
        "提醒:会话失效期间该账号的消息不会被监听、不会写表、不会预警。"
    )
    return head + body


def session_hijacked_alert(phone, account_name, inspector_mention="", host_ip="", company_display=""):
    """v3.0.17 新增:异地登录(被盗号)专用预警 — 跟普通 revoked 区分,提示客户高优先级处理。

    判定依据:Telethon 调 RPC 时抛 AuthKeyDuplicated → 同一账号在另一台机器登录,
    本地 session 自动作废。这跟「自己点终止其他会话」不一样,是被盗号的强信号。

    所有用户填的字段都做 HTML escape — bot.send_session_alert 用 parse_mode='HTML' 推。"""
    tag = _vps_tag(host_ip, company_display)
    name_safe, phone_safe = _safe_name(account_name, phone)
    head = f"【⚠ 外事号异地登录 · {tag}】\n\n"
    body = (
        f"外事号:{name_safe} ({phone_safe})\n"
        f"状态:🔥 检测到异地登录(本地 session 已被踢)\n"
    )
    if inspector_mention:
        body += f"监察员:{inspector_mention}\n"
    body += (
        "\n"
        "判定依据:Telegram 服务端返回 AuthKeyDuplicated\n"
        "  → 同一账号在另一台设备/机器上登录,本地 session 被强制踢下线。\n\n"
        "高风险!立刻处理(顺序很重要):\n"
        "  1) 打开 TG 官方 App → 设置 → 设备 → 终止所有其他会话\n"
        "  2) 改两步验证密码(设置 → 隐私和安全 → 两步验证)\n"
        "  3) Web 后台重新登录该账号(等于把 session 抢回来)\n\n"
        "⚠ 在你抢回前,异地那个 session 仍能看你聊天 + 发消息。"
    )
    return head + body


def session_restored_alert(phone, account_name, host_ip="", company_display=""):
    """v3.0.17(was v2.10.4): session 恢复正常 — 顺手把部门标签也带上,跟 revoked 头对头。

    所有用户填的字段都做 HTML escape — bot.send_session_alert 用 parse_mode='HTML' 推。"""
    tag = _vps_tag(host_ip, company_display)
    name_safe, phone_safe = _safe_name(account_name, phone)
    return (
        f"【外事号恢复通知 · {tag}】\n\n"
        f"外事号:{name_safe} ({phone_safe})\n"
        f"状态:✅ 监听已恢复正常"
    )
