"""推送消息模板 — 按商务提供的格式"""
import config


def no_reply_alert(company, operator, account_name, peer_name, message_text):
    return (
        f"【信息未回复预警{config.COMPANY_DISPLAY}】\n\n"
        f"中心/部门：{company}\n"
        f"商务人员：{operator}\n"
        f"外事号：{account_name}\n"
        f"{config.PEER_ROLE_LABEL}：{peer_name}\n"
        f"未回复信息：{message_text}"
    )


def delete_alert(company, operator, account_name, peer_name, message_text=""):
    text = (
        f"【信息删除预警{config.COMPANY_DISPLAY}】\n\n"
        f"中心/部门：{company}\n"
        f"商务人员：{operator}\n"
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
        f"商务人员：{operator}\n"
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
