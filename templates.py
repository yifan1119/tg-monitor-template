"""推送消息模板 — 按商务提供的格式"""
import html as _html
import config


def no_reply_alert(company, operator, account_name, peer_name, message_text):
    return (
        f"【信息未回复预警{config.COMPANY_DISPLAY}】\n\n"
        f"中心/部门：{company}\n"
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
        f"【信息未回复预警{config.COMPANY_DISPLAY}】\n\n"
        f"中心/部门：{e(company)}\n"
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
        f"【信息未回复升级{config.COMPANY_DISPLAY}】\n\n"
        f"中心/部门：{e(company)}\n"
        f"{config.OPERATOR_LABEL}：{e(operator)}\n"
        f"外事号：{e(account_name)}\n"
        f"{config.PEER_ROLE_LABEL}：{e(peer_name)}\n"
        f"未回复信息：{e(message_text)}"
    )
    tail_text = e(custom_text.strip()) if custom_text else "已超过 40 分钟未回复,请处理"
    if owner_mention:
        return f"{base}\n\n{owner_mention} {tail_text}"
    return f"{base}\n\n{tail_text}"


def delete_alert(company, operator, account_name, peer_name, message_text=""):
    text = (
        f"【信息删除预警{config.COMPANY_DISPLAY}】\n\n"
        f"中心/部门：{company}\n"
        f"{config.OPERATOR_LABEL}：{operator}\n"
        f"外事号：{account_name}\n"
        f"{config.PEER_ROLE_LABEL}：{peer_name}"
    )
    if message_text:
        text += f"\n已删除信息：{message_text}"
    return text


def keyword_alert(company, operator, account_name, peer_name, keyword, message_text):
    return (
        f"【关键词监听{config.COMPANY_DISPLAY}】\n\n"
        f"中心/部门：{company}\n"
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

def session_revoked_alert(phone, account_name):
    """v2.10.4: TG 会话被吊销(用户在 TG 官方 App 点「终止其他会话」会触发)"""
    return (
        f"【外事号离线预警{config.COMPANY_DISPLAY}】\n\n"
        f"外事号:{account_name or '—'} ({phone})\n"
        f"状态:❌ 登录会话已失效\n\n"
        f"可能原因:\n"
        f"  • 你在 TG 官方 App「设置→设备」点了「终止其他会话」\n"
        f"  • 账号被 TG 风控封禁或限制登录\n"
        f"  • Session 文件损坏\n\n"
        f"处理方式:\n"
        f"  1) 打开 Web 后台 → 账号管理 → 重新登录该账号\n"
        f"  2) 输入验证码完成登录即可恢复监听\n\n"
        f"提醒:会话失效期间该账号的消息不会被监听、不会写表、不会预警。"
    )


def session_restored_alert(phone, account_name):
    """v2.10.4: session 恢复正常"""
    return (
        f"【外事号恢复通知{config.COMPANY_DISPLAY}】\n\n"
        f"外事号:{account_name or '—'} ({phone})\n"
        f"状态:✅ 监听已恢复正常"
    )
