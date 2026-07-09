#!/usr/bin/env python3
"""合体v5.1 每日操盘提醒 — 零依赖(纯标准库), 供 GitHub Actions / 本地 cron 每晚运行.

逻辑:
  1. 拉取轮动三标的(纳指513100/黄金518880/国债511010)前复权日线(腾讯);
  2. 从头重放双份轮动(每月第1/11交易日, L15动量+现金分), 得到两份当前应持仓;
  3. 判断下一交易日是否为调仓日, 若是给出 卖旧@9:15跌停价 / 买新@9:31五档价 指令;
  4. 纳指溢价(最新收盘 vs 最新净值)>8% 则调仓时剔除纳指, 7~9%提示明早复核;
  5. 做T腿: 每晚提醒核对6张条件单(9:15涨停价买/14:58跌停价卖), 月初提示rebase与闸门;
  6. 下一交易日是周三 → 提醒定投转入;
  7. 有 SERVERCHAN_KEY 环境变量则推微信(Server酱), 否则打印到stdout.

口径与 JQ 策略「合体v5.1-终版」完全一致; 参数冻结, 勿改.
"""
import json, os, datetime, urllib.request, urllib.parse
from zoneinfo import ZoneInfo

# ======== 配置区(仅此处可改) ========
# 做T腿每日条件单数量(股)。月初 rebase 只上调: 新批量 = int(账户总值*0.5*w*0.49/现价/100)*100
# w: 510500=0.5, 512800=0.3, 563300=0.2。当前为模拟盘1万元口径, 实盘入金后按上式更新。
LOTS = {'510500 中证500ETF': 100, '512800 银行ETF': 900, '563300 中证2000ETF': 300}
# ===================================

L = 15
CASH_SCORE = 0.015 / 243 * L
PREM_TH = 0.08
ROT = ['sh513100', 'sh518880', 'sh511010']
TLEG = ['sh510500', 'sh512800', 'sh563300']
NAME = {'sh513100': '纳指ETF 513100', 'sh518880': '黄金ETF 518880',
        'sh511010': '国债ETF 511010', 'CASH': '现金(逆回购)'}
CST = ZoneInfo('Asia/Shanghai')

def http_get(url, referer=None):
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0',
                                               **({'Referer': referer} if referer else {})})
    return urllib.request.urlopen(req, timeout=20).read().decode('utf-8', 'ignore')

def fetch_qfq(code, n=640):
    """腾讯前复权日线 -> [(date, open, close), ...] 升序; 剔除未收盘的当日bar"""
    out = http_get(f'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,,,{n},qfq')
    d = json.loads(out)['data'][code]
    rows = d.get('qfqday') or d.get('day')
    now = datetime.datetime.now(CST)
    drop = now.strftime('%Y-%m-%d') if now.hour * 60 + now.minute < 15 * 60 + 5 else ''
    return [(r[0], float(r[1]), float(r[2])) for r in rows if r[0] != drop]

def fetch_raw_price(code):
    """腾讯实时行情取未复权最新价(收盘后=当日收盘价), 失败返回None"""
    try:
        out = http_get(f'https://qt.gtimg.cn/q={code}')
        p = float(out.split('~')[3])
        return p if p > 0 else None
    except Exception:
        return None

def fetch_nav_513100():
    """新浪基金接口取513100最新单位净值 -> (date, nav) 或 None"""
    try:
        out = http_get('https://stock.finance.sina.com.cn/fundInfo/api/openapi.php/'
                       'CaihuiFundInfoService.getNav?symbol=513100&page=1',
                       referer='https://finance.sina.com.cn')
        rows = json.loads(out)['result']['data']['data']
        r = rows[0]
        return r['fbrq'][:10], float(r['jjjz'])
    except Exception:
        return None

def replay_rotation(closes, dates):
    """重放双份轮动, 返回 hold[2], 各份上次调仓日, 月内交易日序号表"""
    tday, lastm, cnt = {}, None, 0
    for d in dates:
        if d[:7] != lastm:
            lastm, cnt = d[:7], 0
        cnt += 1
        tday[d] = cnt
    hist = {s: [] for s in ROT}
    hold = [None, None]
    last_reb = [None, None]
    for i, d in enumerate(dates):
        for leg, rb in ((0, 1), (1, 11)):
            if tday[d] != rb:
                continue
            sc = {s: hist[s][-1] / hist[s][-L] - 1 for s in ROT if len(hist[s]) >= L}
            if not sc:
                continue
            sc['CASH'] = CASH_SCORE
            best = max(sc, key=sc.get)
            hold[leg] = best
            last_reb[leg] = d
        for s in ROT:
            hist[s].append(closes[s][d])
    return hold, last_reb, tday

def next_weekday(day):
    d = day + datetime.timedelta(days=1)
    while d.weekday() >= 5:
        d += datetime.timedelta(days=1)
    return d

def main():
    today = datetime.datetime.now(CST).date()
    nxt = next_weekday(today)
    K = {}
    for code in ROT:
        K[code] = fetch_qfq(code)
    dates = sorted(set.intersection(*[set(d for d, _, _ in K[c]) for c in ROT]))
    closes = {c: {d: cl for d, _, cl in K[c]} for c in ROT}
    hold, last_reb, tday = replay_rotation(closes, dates)
    D = dates[-1]                      # 最新已收盘交易日
    k = tday[D]                        # D 是当月第k个交易日
    nxt_tday = 1 if nxt.month != int(D[5:7]) else k + 1

    # 最新动量榜
    sc = {s: closes[s][dates[-1]] / closes[s][dates[-1 - L + 1]] - 1 for s in ROT}
    sc['CASH'] = CASH_SCORE
    rank = sorted(sc, key=sc.get, reverse=True)

    # 溢价
    prem_line, prem_block = '', False
    nav = fetch_nav_513100()
    px = fetch_raw_price('sh513100') or closes['sh513100'][D]
    if nav:
        prem = px / nav[1] - 1
        prem_block = prem > PREM_TH
        warn = ' ⚠️临界,明早9:25用竞价价复核' if 0.07 <= prem <= 0.09 else ''
        prem_line = f'纳指溢价 {prem:+.1%} (价{px:.3f}/净值{nav[1]:.3f}@{nav[0]}){warn}'
        if prem_block:
            prem_line += ' → 触发8%闸, 今次调仓剔除纳指'
    else:
        prem_line = '纳指溢价: 接口未取到, 若调仓涉及纳指请在App核对IOPV溢价<8%'

    # 做T闸门(250日隔夜均值<-2bp)
    gate = []
    for code in TLEG:
        rows = fetch_qfq(code, 300)
        ov = [rows[i][1] / rows[i - 1][2] - 1 for i in range(1, len(rows))]
        m = sum(ov[-250:]) / min(250, len(ov)) * 1e4
        gate.append((code, m, m < -2.0))

    # ---- 组装消息 ----
    lines = [f'📅 下一交易日: {nxt}(当月第{nxt_tday}个交易日)', '']
    # 轮动
    if nxt_tday in (1, 11):
        leg = 0 if nxt_tday == 1 else 1
        cur = hold[leg]
        sc2 = dict(sc)
        if prem_block and 'sh513100' in sc2:
            del sc2['sh513100']
        best = max(sc2, key=sc2.get)
        lines.append(f'🔄 明天是调仓日! 第{leg + 1}份轮动仓位(当前持有 {NAME.get(cur, "空")}):')
        if best == cur:
            lines.append(f'→ 信号={NAME[best]}, 与当前一致, **不动**')
        else:
            if cur not in (None, 'CASH'):
                lines.append(f'→ 卖出 {NAME[cur]}: 定时条件单 9:15 @跌停价(开盘价成交)')
            if best != 'CASH':
                lines.append(f'→ 买入 {NAME[best]}: 定时条件单 9:31 @五档价, 数量=可用资金÷现价 取整百')
            else:
                lines.append('→ 转现金: 卖出后资金做逆回购/通用回购')
    else:
        nd11 = 11 - nxt_tday if nxt_tday < 11 else None
        eta = f'约{nd11}个交易日后到第11交易日调仓' if nd11 else '下月第1交易日调仓'
        lines.append(f'🔄 轮动: 明天不是调仓日({eta}), 无动作')
    lines.append('当前应持仓: 第1份=' + NAME.get(hold[0], '?') + f'({last_reb[0]}起) 第2份=' +
                 NAME.get(hold[1], '?') + f'({last_reb[1]}起)')
    lines.append('L15动量榜: ' + ' > '.join(f'{NAME[s].split(" ")[0]}{sc[s]:+.1%}' for s in rank))
    lines.append(prem_line)
    lines.append('')
    # 做T
    off = [c for c, m, a in gate if not a]
    lines.append('🔁 做T条件单核对(每天): 9:15买@涨停价 + 14:58卖@跌停价, 各标的数量:')
    for name_, lot in LOTS.items():
        flag = ' ⛔闸门关闭,本月暂停做T,只留底仓' if any(name_.startswith(c[2:]) for c in off) else ''
        lines.append(f'  {name_}: {lot}股{flag}')
    lines.append('闸门(250日隔夜bp): ' + ' '.join(f'{c[2:]}{m:+.1f}' for c, m, a in gate))
    lines.append('')
    if nxt.weekday() == 2:
        lines.append('💰 明天周三: 定投转入 1000 元')
    if nxt_tday == 1:
        lines.append('🗓️ 明天月初: 做T批量rebase(只上调) 新批量=int(总值×0.5×w×0.49/现价/100)×100; 核对闸门状态')
    body = '\n'.join(lines)
    title = ('🔄调仓日! ' if nxt_tday in (1, 11) else '') + f'合体v5.1 {nxt} 操盘单'

    key = os.environ.get('SERVERCHAN_KEY', '')
    if key:
        data = urllib.parse.urlencode({'title': title, 'desp': body}).encode()
        req = urllib.request.Request(f'https://sctapi.ftqq.com/{key}.send', data=data)
        print(urllib.request.urlopen(req, timeout=20).read().decode()[:200])
    print(title + '\n' + body)

if __name__ == '__main__':
    main()
