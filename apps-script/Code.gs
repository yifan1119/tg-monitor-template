/**
 * TG 监控看板 — Google Apps Script 后端
 *
 * 绑定在部门的监控 Google Sheet 上(扩展功能 → Apps Script)。
 * 部署为 Web App 后,每个部门主管拿到自己的 URL 即可查看看板。
 *
 * 零配置 — 部门后缀(suffix)从预警分页标题自动识别,
 * 所以这份代码无需针对任何部门修改,各部门 Sheet 粘贴即用。
 */

// ========== Web App 入口 ==========
function doGet(e) {
  return HtmlService.createTemplateFromFile('Dashboard')
    .evaluate()
    .setTitle('TG 监控看板')
    .addMetaTag('viewport', 'width=device-width, initial-scale=1')
    .setXFrameOptionsMode(HtmlService.XFrameOptionsMode.ALLOWALL);
}

// 前端调用,一次拿全部数据
function getDashboardData() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const suffix = detectSuffix(ss);

  const alertTabs = {
    noReply: ss.getSheetByName('信息未回复预警' + suffix),
    keyword: ss.getSheetByName('关键词监听' + suffix),
    deleted: ss.getSheetByName('信息删除预警' + suffix),
  };
  const accountTabs = getAccountTabs(ss, suffix);

  // 所有预警分页的行(带时间)预先聚合一次,后面多处复用
  const noReplyRows = readAlertRows(alertTabs.noReply, 5);     // 时间在第 6 列(0-based 5)
  const keywordRows = readAlertRows(alertTabs.keyword, 6);     // 时间在第 7 列(0-based 6)
  // 删除分页:新版 6 列(第 5 列 message,第 6 列时间),旧版 5 列(第 5 列时间)
  const deletedRows = readDeletedRows(alertTabs.deleted);

  const conversationStats = scanConversations(accountTabs);

  return {
    company: suffix || '(未识别)',
    spreadsheetName: ss.getName(),
    generatedAt: nowStr(),
    accountCount: accountTabs.length,
    kpis: buildKpis(noReplyRows, keywordRows, deletedRows, conversationStats),
    trend7d: buildTrend7d(noReplyRows, keywordRows, deletedRows, conversationStats),
    hourHeatmap: conversationStats.hourHeatmap,
    keywordTop: buildKeywordTop(keywordRows),
    operatorStats: buildOperatorStats(noReplyRows, keywordRows, deletedRows, conversationStats),
    slowResponders: buildSlowResponders(conversationStats.responseLog),
    accountActivity: conversationStats.accountActivity.slice(0, 10),
    inOutStats: buildInOutStats(conversationStats),
    recentAlerts: buildRecentAlerts(noReplyRows, keywordRows, deletedRows),
    alertsNoReply: buildAlertsList(noReplyRows, 'no_reply').slice(0, 30),
    alertsKeyword: buildAlertsList(keywordRows, 'keyword').slice(0, 30),
    alertsDeleted: buildAlertsList(deletedRows, 'deleted').slice(0, 30),
  };
}

// ========== 工具函数 ==========
function detectSuffix(ss) {
  // 从预警分页标题推断部门 suffix
  // 例如 "信息未回复预警悦达" → suffix = "悦达"
  const prefix = '信息未回复预警';
  const sheets = ss.getSheets();
  for (let i = 0; i < sheets.length; i++) {
    const name = sheets[i].getName();
    if (name.indexOf(prefix) === 0) {
      return name.substring(prefix.length);
    }
  }
  return '';
}

function getAccountTabs(ss, suffix) {
  // 对话 tab = 除预警分页和首页 "总览/Dashboard" 外的 tab
  const excludes = {
    ['信息未回复预警' + suffix]: 1,
    ['关键词监听' + suffix]: 1,
    ['信息删除预警' + suffix]: 1,
    'Dashboard': 1, '总览': 1, '看板': 1,
  };
  return ss.getSheets().filter(function (s) {
    return !excludes[s.getName()];
  });
}

function readAlertRows(sheet, tsColIdx) {
  if (!sheet) return [];
  const last = sheet.getLastRow();
  if (last < 2) return [];
  const vals = sheet.getRange(1, 1, last, Math.max(sheet.getLastColumn(), tsColIdx + 1)).getValues();
  const out = [];
  for (let i = 0; i < vals.length; i++) {
    const row = vals[i];
    const ts = row[tsColIdx];
    if (!ts) continue;
    const dt = parseTs(ts);
    if (!dt) continue;
    out.push({ row: row, ts: dt });
  }
  return out;
}

function readDeletedRows(sheet) {
  // 兼容两种结构:
  //   新(6 列): 公司 | 商务 | 外事号 | 广告主 | 删除前消息 | 时间
  //   旧(5 列): 公司 | 商务 | 外事号 | 广告主 | 时间
  if (!sheet) return [];
  const last = sheet.getLastRow();
  if (last < 1) return [];
  const lastCol = Math.max(sheet.getLastColumn(), 6);
  const vals = sheet.getRange(1, 1, last, lastCol).getValues();
  const out = [];
  for (let i = 0; i < vals.length; i++) {
    const row = vals[i];
    // 先尝试第 6 列(idx 5)作为时间 - 新格式
    let dt = parseTs(row[5]);
    let text = '';
    if (dt) {
      text = String(row[4] || '');
    } else {
      // 退回第 5 列(idx 4)作为时间 - 旧格式
      dt = parseTs(row[4]);
      text = '';  // 旧格式没消息
    }
    if (!dt) continue;
    out.push({ row: row, ts: dt, text: text });
  }
  return out;
}

function parseTs(val) {
  if (val instanceof Date) return val;
  if (!val) return null;
  const s = String(val).trim();
  // 支持 "YYYY-MM-DD HH:MM:SS" / "YYYY/MM/DD HH:MM" 等
  const m = s.match(/^(\d{4})[-\/](\d{1,2})[-\/](\d{1,2})[ T](\d{1,2}):(\d{1,2})(?::(\d{1,2}))?/);
  if (m) {
    return new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +(m[6] || 0));
  }
  const d = new Date(s);
  return isNaN(d.getTime()) ? null : d;
}

function ymd(d) {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return y + '-' + m + '-' + day;
}

function nowStr() {
  return Utilities.formatDate(new Date(), 'Asia/Shanghai', 'yyyy-MM-dd HH:mm:ss');
}

function isToday(d) {
  const now = new Date();
  return d.getFullYear() === now.getFullYear()
    && d.getMonth() === now.getMonth()
    && d.getDate() === now.getDate();
}

// ========== 对话扫描(一次扫完,所有需要基于对话的指标共用) ==========
// 自动兼容两种对话分页格式:
//   format A (新模板): row 7+ 数据, 每 3 列一组 [时间/方向/文本], 广告主名在 row 6 col N+2
//   format B (老部门): row 4+ 数据, 每 4 列一组 [时间/方向/文本/空], 广告主名在 row 3 col N+3
function detectTabFormat(ws) {
  // 优先尝试 format A
  const r6c3 = ws.getRange(6, 3).getValue();
  const r7c1 = ws.getRange(7, 1).getValue();
  if (r6c3 && parseTs(r7c1)) {
    return { dataStartRow: 7, colStride: 3, peerRow: 6, peerOffset: 2 };
  }
  // 退回 format B
  const r3c4 = ws.getRange(3, 4).getValue();
  const r4c1 = ws.getRange(4, 1).getValue();
  if (r3c4 && parseTs(r4c1)) {
    return { dataStartRow: 4, colStride: 4, peerRow: 3, peerOffset: 3 };
  }
  return null;
}

function scanConversations(accountTabs) {
  const hourHeatmap = new Array(24).fill(0);            // 整体每小时消息数
  const responseLog = [];                                // {account, peer, waitMin}
  const perDayMsgs = {};                                 // date -> count
  const activeConversationsToday = {};                   // account::peer
  const accountActivityMap = {};                         // name -> {sent, recv, peers:Set}
  const operatorResponseLog = {};                        // operator -> {sum, count}
  const operatorTotalRecv = {};                          // operator -> incoming msgs
  const operatorRepliedRecv = {};                        // operator -> incoming with reply
  let totalMsgs = 0;
  let totalSent = 0;
  let totalRecv = 0;

  for (let k = 0; k < accountTabs.length; k++) {
    const ws = accountTabs[k];
    const accountName = ws.getName();
    const lastRow = ws.getLastRow();
    const lastCol = ws.getLastColumn();
    if (lastRow < 4 || lastCol < 3) continue;

    const fmt = detectTabFormat(ws);
    if (!fmt) continue;

    // 一次性读数据区 + 表头行
    const data = ws.getRange(fmt.dataStartRow, 1, lastRow - fmt.dataStartRow + 1, lastCol).getValues();
    const peerRow = ws.getRange(fmt.peerRow, 1, 1, lastCol).getValues()[0];

    const groupCount = Math.floor(lastCol / fmt.colStride);
    for (let g = 0; g < groupCount; g++) {
      const baseCol = g * fmt.colStride;
      const peerName = peerRow[baseCol + fmt.peerOffset] || '';
      if (!peerName) continue;

      if (!accountActivityMap[accountName]) {
        accountActivityMap[accountName] = { name: accountName, sent: 0, recv: 0, peers: {} };
      }
      accountActivityMap[accountName].peers[peerName] = 1;

      // 扫这组三列(时间/方向/文本)
      let lastIncomingTs = null;
      for (let r = 0; r < data.length; r++) {
        const ts = data[r][baseCol];
        const dir = data[r][baseCol + 1];
        const text = data[r][baseCol + 2];
        if (!ts) continue;
        const dt = parseTs(ts);
        if (!dt) continue;

        totalMsgs++;
        hourHeatmap[dt.getHours()]++;

        const dayKey = ymd(dt);
        perDayMsgs[dayKey] = (perDayMsgs[dayKey] || 0) + 1;

        if (isToday(dt)) {
          activeConversationsToday[accountName + '::' + peerName] = 1;
        }

        // 回复时长:对方消息(B/接收/incoming) → 下一条我方消息
        const dirStr = String(dir || '');
        const isIncoming = dirStr.indexOf('B') === 0 || dirStr.indexOf('入') === 0 || dirStr.indexOf('接') === 0 || dirStr === '对方';
        const isOutgoing = dirStr.indexOf('A') === 0 || dirStr.indexOf('出') === 0 || dirStr.indexOf('发') === 0 || dirStr === '我方';

        if (isIncoming) {
          totalRecv++;
          accountActivityMap[accountName].recv++;
          if (!lastIncomingTs) lastIncomingTs = dt;  // 只记录首条未回复,避免重复
        } else if (isOutgoing) {
          totalSent++;
          accountActivityMap[accountName].sent++;
          if (lastIncomingTs) {
            const waitMin = (dt.getTime() - lastIncomingTs.getTime()) / 60000;
            if (waitMin >= 0 && waitMin < 24 * 60) {   // 过滤跨天异常
              responseLog.push({
                account: accountName,
                peer: peerName,
                waitMin: waitMin,
                at: dt,
              });
            }
            lastIncomingTs = null;
          }
        }
      }
    }
  }

  // 活跃度: peers 数 + 排行
  const accountActivity = Object.keys(accountActivityMap).map(function (name) {
    const a = accountActivityMap[name];
    return { name: name, sent: a.sent, recv: a.recv, peers: Object.keys(a.peers).length };
  }).sort(function (a, b) { return (b.sent + b.recv) - (a.sent + a.recv); });

  return {
    hourHeatmap: hourHeatmap,
    responseLog: responseLog,
    perDayMsgs: perDayMsgs,
    activeConversationsTodayCount: Object.keys(activeConversationsToday).length,
    totalMsgs: totalMsgs,
    totalSent: totalSent,
    totalRecv: totalRecv,
    accountActivity: accountActivity,
  };
}

// ========== KPI 卡 ==========
function buildKpis(noReplyRows, keywordRows, deletedRows, conv) {
  const now = new Date();
  const yesterdayKey = ymd(new Date(now.getFullYear(), now.getMonth(), now.getDate()-1));
  const isYesterday = function(d) { return ymd(d) === yesterdayKey; };
  const countToday = function(arr) { return arr.filter(function(x){ return isToday(x.ts); }).length; };
  const countYesterday = function(arr) { return arr.filter(function(x){ return isYesterday(x.ts); }).length; };

  const recent = conv.responseLog.filter(function (x) { return isToday(x.at); });
  const avgResp = recent.length ? (recent.reduce(function (a,b) { return a + b.waitMin; }, 0) / recent.length) : 0;
  const responseRate = conv.totalRecv > 0 ? Math.round(conv.responseLog.length / conv.totalRecv * 100) : 0;

  return {
    todayMessages: conv.perDayMsgs[ymd(now)] || 0,
    yesterdayMessages: conv.perDayMsgs[yesterdayKey] || 0,
    todayConversations: conv.activeConversationsTodayCount,
    yesterdayConversations: countYesterday(noReplyRows) + countYesterday(keywordRows),
    todayNoReply: countToday(noReplyRows),
    yesterdayNoReply: countYesterday(noReplyRows),
    todayKeyword: countToday(keywordRows),
    yesterdayKeyword: countYesterday(keywordRows),
    todayDeleted: countToday(deletedRows),
    yesterdayDeleted: countYesterday(deletedRows),
    avgResponseMin: Math.round(avgResp * 10) / 10,
    responseRate: responseRate,
  };
}

// ========== 7 天趋势 ==========
function buildTrend7d(noReplyRows, keywordRows, deletedRows, conv) {
  const days = [];
  const now = new Date();
  for (let i = 6; i >= 0; i--) {
    const d = new Date(now.getFullYear(), now.getMonth(), now.getDate() - i);
    days.push(ymd(d));
  }

  function countByDay(rows) {
    const m = {};
    rows.forEach(function (x) { const k = ymd(x.ts); m[k] = (m[k] || 0) + 1; });
    return days.map(function (d) { return m[d] || 0; });
  }

  return {
    labels: days.map(function (d) { return d.slice(5); }),  // MM-DD
    noReply: countByDay(noReplyRows),
    keyword: countByDay(keywordRows),
    deleted: countByDay(deletedRows),
    messages: days.map(function (d) { return conv.perDayMsgs[d] || 0; }),
  };
}

// ========== 关键词 Top ==========
function buildKeywordTop(keywordRows) {
  const counter = {};
  keywordRows.forEach(function (x) {
    // 关键词监听列:公司/商务/外事号/广告主/关键词/消息/时间 → idx 4
    const kw = x.row[4];
    if (!kw) return;
    counter[kw] = (counter[kw] || 0) + 1;
  });
  return Object.keys(counter)
    .map(function (k) { return { keyword: k, count: counter[k] }; })
    .sort(function (a, b) { return b.count - a.count; })
    .slice(0, 10);
}

// ========== 商务人员排行 ==========
function buildOperatorStats(noReplyRows, keywordRows, deletedRows, conv) {
  // 预警行第 2 列 = 商务人员(idx 1)
  const m = {};
  function bump(op, field) {
    if (!op) return;
    if (!m[op]) m[op] = { operator: op, noReply: 0, keyword: 0, deleted: 0 };
    m[op][field]++;
  }
  noReplyRows.forEach(function (x) { bump(x.row[1], 'noReply'); });
  keywordRows.forEach(function (x) { bump(x.row[1], 'keyword'); });
  deletedRows.forEach(function (x) { bump(x.row[1], 'deleted'); });

  return Object.values(m).map(function (o) {
    o.total = o.noReply + o.keyword + o.deleted;
    // 响应率 ~= 1 - 未回复告警 / (未回复+关键词) * 100 (粗略近似)
    const base = o.total || 1;
    o.responseRate = Math.max(0, Math.min(100, Math.round((1 - o.noReply / base) * 100)));
    return o;
  }).sort(function (a, b) { return b.total - a.total; }).slice(0, 15);
}

// ========== 3 栏告警列表(按类型返回) ==========
function buildAlertsList(rows, type) {
  return rows.slice().sort(function (a, b) { return b.ts.getTime() - a.ts.getTime(); })
    .map(function (x) {
      const base = {
        type: type,
        time: Utilities.formatDate(x.ts, 'Asia/Shanghai', 'MM-dd HH:mm'),
        account: x.row[2] || '',
        peer: x.row[3] || '',
      };
      if (type === 'no_reply') {
        base.text = String(x.row[4] || '').slice(0, 120);
        const waitMin = Math.round((new Date().getTime() - x.ts.getTime()) / 60000);
        base.wait = waitMin < 60 ? (waitMin + ' min') : (waitMin < 1440 ? Math.round(waitMin/60*10)/10 + 'h' : Math.round(waitMin/1440) + 'd');
      } else if (type === 'keyword') {
        base.kw = x.row[4] || '';
        base.text = String(x.row[5] || '').slice(0, 120);
      } else {  // deleted
        base.text = String(x.text || '').slice(0, 120);  // 来自 readDeletedRows
        base.isLegacy = !x.text;                          // 旧格式(5列)没删除前内容
      }
      return base;
    });
}

// ========== 回复时长排行(慢响应 Top 10) ==========
function buildSlowResponders(responseLog) {
  // 按广告主聚合平均回复时长
  const m = {};
  responseLog.forEach(function (x) {
    const k = x.account + ' → ' + x.peer;
    if (!m[k]) m[k] = { key: k, account: x.account, peer: x.peer, total: 0, n: 0, max: 0 };
    m[k].total += x.waitMin;
    m[k].n++;
    if (x.waitMin > m[k].max) m[k].max = x.waitMin;
  });
  return Object.values(m)
    .map(function (v) { return { key: v.key, account: v.account, peer: v.peer, avg: Math.round(v.total / v.n * 10) / 10, max: Math.round(v.max * 10) / 10, count: v.n }; })
    .sort(function (a, b) { return b.avg - a.avg; })
    .slice(0, 10);
}

// ========== 最新 10 条告警 ==========
function buildRecentAlerts(noReplyRows, keywordRows, deletedRows) {
  const merged = [];

  noReplyRows.forEach(function (x) {
    merged.push({
      type: 'no_reply', ts: x.ts,
      account: x.row[2], peer: x.row[3], text: x.row[4],
    });
  });
  keywordRows.forEach(function (x) {
    merged.push({
      type: 'keyword', ts: x.ts,
      account: x.row[2], peer: x.row[3], text: '[' + x.row[4] + '] ' + (x.row[5] || ''),
    });
  });
  deletedRows.forEach(function (x) {
    // 新版有删除前消息,旧版没有
    const deletedText = x.text ? x.text : '(旧记录无消息内容)';
    merged.push({
      type: 'deleted', ts: x.ts,
      account: x.row[2], peer: x.row[3], text: deletedText,
    });
  });

  return merged
    .sort(function (a, b) { return b.ts.getTime() - a.ts.getTime(); })
    .slice(0, 10)
    .map(function (x) {
      return {
        type: x.type,
        time: Utilities.formatDate(x.ts, 'Asia/Shanghai', 'MM-dd HH:mm'),
        account: x.account || '', peer: x.peer || '',
        text: String(x.text || '').slice(0, 80),
      };
    });
}

// ========== 菜单(方便在 Sheet 里打开看板) ==========
function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu('TG 监控')
    .addItem('打开看板', 'openDashboardSidebar')
    .addItem('部署说明', 'showDeployHelp')
    .addToUi();
}

function openDashboardSidebar() {
  const html = HtmlService.createTemplateFromFile('Dashboard')
    .evaluate()
    .setTitle('TG 监控看板')
    .setWidth(1200);
  SpreadsheetApp.getUi().showModalDialog(html, 'TG 监控看板');
}

function showDeployHelp() {
  const html = HtmlService.createHtmlOutput(
    '<div style="font-family:system-ui;padding:16px;line-height:1.6">' +
    '<h3 style="margin-top:0">部署成 Web App(给主管独立链接)</h3>' +
    '<ol>' +
    '<li>顶部 <b>部署</b> → <b>新建部署</b></li>' +
    '<li>类型选 <b>网络应用</b></li>' +
    '<li>执行身份:<b>我</b>,访问权限:<b>任何拥有链接的人</b></li>' +
    '<li>点 <b>部署</b>,复制出来的 URL 给主管</li>' +
    '</ol>' +
    '<p style="color:#666">代码更新后要重新走一次部署流程并选「管理部署」→ 铅笔编辑 → 新版本,URL 不变。</p>' +
    '</div>'
  ).setWidth(520).setHeight(320);
  SpreadsheetApp.getUi().showModalDialog(html, '部署说明');
}
