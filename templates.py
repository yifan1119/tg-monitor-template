"""推送消息模板 — 按商务提供的格式"""
import config


def no_reply_alert(company, operator, account_name, peer_name, message_text):
    return (
        f"【信息未回复预警{config.COMPANY_DISPLAY}】\n\n"
        f"中心/部门：{company}\n"
        f"商务人员：{operator}\n"
        f"外事号：{account_name}\n"
        f"广告主：{peer_name}\n"
        f"未回复信息：{message_text}"
    )


def delete_alert(company, operator, account_name, peer_name, message_text=""):
    text = (
        f"【信息删除预警{config.COMPANY_DISPLAY}】\n\n"
        f"中心/部门：{company}\n"
        f"商务人员：{operator}\n"
        f"外事号：{account_name}\n"
        f"广告主：{peer_name}"
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
        f"广告主：{peer_name}\n"
        f"关键词：{keyword}\n"
        f"消息内容：{message_text}"
    )


def daily_report(report_date, record_time, chat_count, no_reply_count, delete_count, keyword_count):
    return (
        f"【外事号监控总结】\n\n"
        f"统计日期：{report_date}\n"
        f"记录时间：{record_time}\n"
        f"监控聊天总数：{chat_count}\n"
        f"未回复数量：{no_reply_count}\n"
        f"信息删除数量：{delete_count}\n"
        f"关键词监听数量：{keyword_count}"
    )
