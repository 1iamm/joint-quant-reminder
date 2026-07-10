#!/usr/bin/env python3
"""合体策略族 每日操盘提醒 — 零依赖(纯标准库), GitHub Actions / 本地 cron 每晚运行.

覆盖三策略(轮动腿三者完全一致: 纳指/黄金/国债/现金, L15动量, 每月第1/11交易日双份错开, 纳指溢价>8%剔除):
  ⭐ v6b: 做T腿 = 银行512800×0.5 + 中证2000 563300×0.5   ← 用户实盘(银河证券)
     v6 : 做T腿 = 银行512800×0.5 + 中证1000 512100×0.5
     v5.1: 做T腿 = 中证500 510500×0.5 + 银行×0.3 + 2000×0.2
做T闸门: 250日隔夜均值<-2bp(逐标的, 月度更新), 三策略共用同一标的状态.
实盘账户预演: 从 live_since 起按协议重放(建仓/每周三定投/轮动含整手买不起退现金),
估算"操作前/操作后"的持仓与现金(估值按最新收盘价, 不含做T日内盈亏与利息).
有 SERVERCHAN_KEY 环境变量则推微信(Server酱, desp为Markdown), 否则打印.
口径与 JQ 各策略终版代码一致; 参数冻结, 勿改.
"""
import json, os, datetime, urllib.request, urllib.parse
from zoneinfo import ZoneInfo

# ======== 配置区(仅此处可改) ========
# 批量=int(资金×0.5×w×0.49/现价/100)×100; 月初rebase只上调。
STRATS = [
    {'key': 'v6b', 'name': 'v6b 银行+中证2000 做T', 'primary': True,
     'live_since': '2026-07-10',   # 实盘建仓日(周五): 当天只买底仓不卖, 次日起每日做T循环
     'capital': 20000,             # 初始入金(2026-07-10 早间转入)
     'dca': 1000,                  # 每周三定投
     'tleg': [('sh512800', '512800 银行ETF', 3200),      # 2万口径
              ('sh563300', '563300 中证2000ETF', 1600)]},
    {'key': 'v6', 'name': 'v6 银行+中证1000 做T', 'primary': False,
     'tleg': [('sh512800', '512800 银行ETF', 1600),
              ('sh512100', '512100 中证1000ETF', 300)]},
    {'key': 'v5.1', 'name': 'v5.1 中证500+银行+2000 做T', 'primary': False,
     'tleg': [('sh510500', '510500 中证500ETF', 100),
              ('sh512800', '512800 银行ETF', 900),
              ('sh563300', '563300 中证2000ETF', 300)]},
]
# ===================================

L = 15
CASH_SCORE = 0.015 / 243 * L
PREM_TH = 0.08
ROT = ['sh513100', 'sh518880', 'sh511010']
TLEG = sorted({c for s in STRATS for c, _, _ in s['tleg']})
NAME = {'sh513100': '纳指ETF 513100', 'sh518880': '黄金ETF 518880',
        'sh511010': '国债ETF 511010', 'CASH': '现金(逆回购)'}
SHORT = {'sh513100': '纳指ETF', 'sh518880': '黄金ETF', 'sh511010': '国债ETF', 'CASH': '现金'}
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
        r = json.loads(out)['result']['data']['data'][0]
        return r['fbrq'][:10], float(r['jjjz'])
    except Exception:
        return None

def replay_rotation(closes, dates):
    """重放双份轮动 -> (hold[2], 各份上次调仓日, 月内交易日序号表, 决策流水[(日,份,目标)])"""
    tday, lastm, cnt = {}, None, 0
    for d in dates:
        if d[:7] != lastm:
            lastm, cnt = d[:7], 0
        cnt += 1
        tday[d] = cnt
    hist = {s: [] for s in ROT}
    hold = [None, None]
    last_reb = [None, None]
    decisions = []
    for d in dates:
        for leg, rb in ((0, 1), (1, 11)):
            if tday[d] != rb:
                continue
            sc = {s: hist[s][-1] / hist[s][-L] - 1 for s in ROT if len(hist[s]) >= L}
            if not sc:
                continue
            sc['CASH'] = CASH_SCORE
            hold[leg] = max(sc, key=sc.get)
            last_reb[leg] = d
            decisions.append((d, leg, hold[leg]))
        for s in ROT:
            hist[s].append(closes[s][d])
    return hold, last_reb, tday, decisions

def simulate_live(p0, opens, closes, dates, decisions):
    """实盘账本重放(估算): 返回 {started, cash, tr_hold[2], tr_qty[2]}; 无live_since返回None.
    规则: live日开盘价买底仓; 此后每周三+定投; 调仓日按信号换仓, 单份预算=24.5%×总值,
    整手(100股)买不起(如国债一手1.4万)自动留现金 — 与策略代码的取整行为一致."""
    live = p0.get('live_since')
    if not live:
        return None
    cash = float(p0.get('capital', 0))
    tr_hold = ['CASH', 'CASH']
    tr_qty = [0, 0]
    started = False
    dec = {}
    for d, leg, tgt in decisions:
        dec.setdefault(d, []).append((leg, tgt))
    base_lots = {c: lot for c, _, lot in p0['tleg']}
    for d in dates:
        if d < live:
            continue
        if not started and d == live:
            for c, lot in base_lots.items():
                cash -= lot * opens[c][d]
        started = True
        if d != live and datetime.date.fromisoformat(d).weekday() == 2:
            cash += p0.get('dca', 0)
        for leg, tgt in dec.get(d, []):
            cur = tr_hold[leg]
            if tgt == cur:
                continue
            if cur != 'CASH':
                cash += tr_qty[leg] * opens[cur][d]
                tr_qty[leg] = 0
                tr_hold[leg] = 'CASH'
            if tgt != 'CASH':
                base_v = sum(lot * closes[c][d] for c, lot in base_lots.items())
                oth = 1 - leg
                oth_v = tr_qty[oth] * closes[tr_hold[oth]][d] if tr_hold[oth] != 'CASH' else 0
                total = cash + base_v + oth_v
                qty = int(min(0.245 * total, cash) / opens[tgt][d] / 100) * 100
                if qty > 0:
                    cash -= qty * opens[tgt][d]
                    tr_hold[leg] = tgt
                    tr_qty[leg] = qty
    return {'started': started, 'cash': cash, 'tr_hold': tr_hold, 'tr_qty': tr_qty}

def next_weekday(day):
    d = day + datetime.timedelta(days=1)
    while d.weekday() >= 5:
        d += datetime.timedelta(days=1)
    return d

def main():
    today = datetime.datetime.now(CST).date()
    nxt = next_weekday(today)
    K = {code: fetch_qfq(code) for code in ROT}
    rdates = sorted(set.intersection(*[set(d for d, _, _ in K[c]) for c in ROT]))
    closes = {c: {d: cl for d, _, cl in K[c]} for c in ROT}
    opens = {c: {d: o for d, o, _ in K[c]} for c in ROT}
    hold, last_reb, tday, decisions = replay_rotation(closes, rdates)
    D = rdates[-1]
    nxt_tday = 1 if nxt.month != int(D[5:7]) else tday[D] + 1

    # 最新动量榜
    sc = {s: closes[s][rdates[-1]] / closes[s][rdates[-L]] - 1 for s in ROT}
    sc['CASH'] = CASH_SCORE
    rank = sorted(sc, key=sc.get, reverse=True)

    # 溢价
    nav = fetch_nav_513100()
    px = fetch_raw_price('sh513100') or closes['sh513100'][D]
    prem_block = False
    if nav:
        prem = px / nav[1] - 1
        prem_block = prem > PREM_TH
        warn = ' ⚠️临界,明早9:25用竞价价复核' if 0.07 <= prem <= 0.09 else ''
        prem_line = f'纳指溢价 **{prem:+.1%}** (价{px:.3f}/净值{nav[1]:.3f}@{nav[0]}){warn}'
        if prem_block:
            prem_line += ' → **触发8%闸, 调仓时剔除纳指**'
    else:
        prem_line = '纳指溢价: 接口未取到, 若调仓涉及纳指请在App核对IOPV溢价<8%'

    # 做T标的行情与闸门(逐标的, 三策略共用)
    gate, tpx = {}, {}
    for code in TLEG:
        rows = fetch_qfq(code, 300)
        ov = [rows[i][1] / rows[i - 1][2] - 1 for i in range(1, len(rows))]
        m = sum(ov[-250:]) / min(250, len(ov)) * 1e4
        gate[code] = (m, m < -2.0)
        opens[code] = {d: o for d, o, _ in rows}
        closes[code] = {d: cl for d, _, cl in rows}
        tpx[code] = rows[-1][2]

    # 实盘账本(主策略)
    p0 = STRATS[0]
    sim = simulate_live(p0, opens, closes, rdates, decisions)
    live = p0.get('live_since', '')
    is_live_day = live == str(nxt)
    pre_live = (not is_live_day) and (sim is not None and not sim['started']) and live > str(today)

    # ---- 组装消息(Markdown; 表格前后需空行) ----
    is_dca = nxt.weekday() == 2

    # 轮动动作判定(三策略同信号, 稳态口径)
    rot_head, rot_steps = None, []
    if nxt_tday in (1, 11):
        leg = 0 if nxt_tday == 1 else 1
        cur = hold[leg]
        sc2 = dict(sc)
        if prem_block and 'sh513100' in sc2:
            del sc2['sh513100']
        best = max(sc2, key=sc2.get)
        if best == cur:
            rot_head = f'调仓日, 但第{leg + 1}份信号与持仓一致({SHORT.get(cur, "?")}), **不动**'
        else:
            rot_head = f'🔄 **第{leg + 1}份换仓: {SHORT.get(cur, "空仓")} → {SHORT[best]}**'
            if cur not in (None, 'CASH'):
                rot_steps.append(f'**9:15** 卖出 {NAME[cur]} 全部持仓, 委托=**跌停价**(集合竞价按开盘价成交)')
            if best != 'CASH':
                rot_steps.append(f'**9:31** 买入 {NAME[best]}, 委托=**五档价**, 数量=可用资金÷现价 取整百')
            else:
                rot_steps.append('卖出后资金停车: **通用回购/逆回购**')

    # 总结行
    off0 = [n.split(' ')[0] for c, n, _ in p0['tleg'] if not gate[c][1]]
    parts = [rot_head if rot_head else '轮动无动作(三策略同)']
    if pre_live:
        parts.append(f'⭐{p0["key"]}**未开跑, 明天仅入金{p0.get("capital", 0)}元**({live} 周一建仓)')
    elif is_live_day:
        parts.append(f'⭐{p0["key"]} **实盘建仓日: 只买底仓{len(p0["tleg"])}张单, 今天不卖**')
    else:
        parts.append(f'⭐{p0["key"]}做T{len(p0["tleg"]) * 2}张单照常' + (f'(⛔{"/".join(off0)}暂停)' if off0 else ''))
    if is_dca:
        parts.append('💰定投1000')
    if nxt_tday == 1:
        parts.append('🗓月初rebase')
    summary = f'**总结**: {nxt} (第{nxt_tday}交易日) — ' + '; '.join(parts)

    def tleg_table(strat):
        rows = ['| 标的 | 批量 | 9:15买 | 14:58卖 | 闸门 |', '| --- | --- | --- | --- | --- |']
        for code, name_, lot in strat['tleg']:
            m, a = gate[code]
            st = '✅开' if a else '⛔停(只留底仓)'
            rows.append(f'| {name_} | {lot}股 | 涨停价 | 跌停价 | {st} {m:+.1f}bp |')
        return rows

    def pos_str(with_double=False):
        """实盘持仓描述(估值=最新收盘)"""
        items = []
        for c, name_, lot in p0['tleg']:
            n = lot * 2 if with_double else lot
            items.append(f'{name_.split(" ")[1]}{n}股(≈{n * tpx[c]:.0f}元)')
        for i in (0, 1):
            if sim['tr_hold'][i] != 'CASH':
                c = sim['tr_hold'][i]
                q = sim['tr_qty'][i]
                items.append(f'{SHORT[c]}{q}股(≈{q * closes[c][D]:.0f}元)')
        return ' + '.join(items) if items else '无'

    md = [summary, '', f'## ⭐ 一、{p0["name"]}(你的实操策略)', '']
    batch_cost = sum(lot * tpx[c] for c, _, lot in p0['tleg'])
    if pre_live:
        md += [f'**准备期**: 明天({nxt})只做一件事——**入金 {p0.get("capital", 0)} 元**, 不下任何单。',
               f'建仓日为 **{live}(周一)**, 周日晚推送会给建仓条件单明细。', '',
               '**账户预演**', '',
               '| 时点 | 持仓 | 现金(约) |', '| --- | --- | --- |',
               f'| 明天入金后 | 无 | {p0.get("capital", 0)}元 |',
               f'| {live}建仓后 | 底仓≈{batch_cost:.0f}元 | ≈{p0.get("capital", 0) - batch_cost:.0f}元 |']
    elif is_live_day:
        md += ['**今天是实盘建仓日**: 只买入底仓, **不设卖单**; 每日做T循环(9:15买批次+14:58卖批次)从下一交易日开始。', '',
               '| 标的 | 底仓数量 | 委托 |', '| --- | --- | --- |']
        for code, name_, lot in p0['tleg']:
            md.append(f'| {name_} | **{lot}股** | 定时条件单 9:15 @涨停价(集合竞价按开盘价成交) |')
        cap = p0.get('capital', 0)
        md += ['', '**账户预演**', '',
               '| 时点 | 持仓 | 现金(约) |', '| --- | --- | --- |',
               f'| 开盘前 | 无 | {cap}元 |',
               f'| 9:15买单成交后 | {" + ".join(f"{n.split(chr(32))[1]}{lot}股(≈{lot * tpx[c]:.0f}元)" for c, n, lot in p0["tleg"])} | ≈{cap - batch_cost:.0f}元 |',
               '',
               f'- ⚠️ **先确认 {cap} 元已到账**; 若银证转账未成, 周一 8:30-9:10 转入后再等 9:15 触发',
               f'- 挂单瞬间按涨停价冻结≈{batch_cost * 1.1:.0f}元, 成交后差额退回',
               '- 其余资金留现金(轮动份额等调仓日信号, 勿抢跑), 收盘前可做**通用回购/逆回购**']
    else:
        md += tleg_table(p0)
        cash_now = sim['cash'] if sim and sim['started'] else 0
        md += ['', '**账户预演(估算价=最新收盘, 现金不含做T盈亏/利息)**', '',
               '| 时点 | 持仓 | 现金(约) |', '| --- | --- | --- |',
               f'| 开盘前 | {pos_str()} | ≈{cash_now:.0f}元 |',
               f'| 日内(9:15批次买入后) | {pos_str(with_double=True)} | ≈{cash_now - batch_cost:.0f}元(9:15挂单需可用≥{batch_cost * 1.1:.0f}) |',
               f'| 收盘(14:58批次卖出后) | {pos_str()} | ≈{cash_now:.0f}元 ± 当日做T盈亏 |']
        if is_dca:
            md.append(f'| 定投到账后 | 不变 | ≈{cash_now + p0.get("dca", 0):.0f}元 |')
        if cash_now < batch_cost * 1.1:
            md += ['', f'⚠️ **资金偏紧**: 可用≈{cash_now:.0f} < 冻结需求≈{batch_cost * 1.1:.0f}, 明早买单请每只各减100股。']
        # 调仓日: 按实盘账本给出真实动作(冷启动/买不起整手都考虑在内)
        if nxt_tday in (1, 11) and sim and sim['started']:
            leg = 0 if nxt_tday == 1 else 1
            cur_r = sim['tr_hold'][leg]
            sc2 = dict(sc)
            if prem_block and 'sh513100' in sc2:
                del sc2['sh513100']
            best_r = max(sc2, key=sc2.get)
            if best_r != 'CASH':
                base_v = sum(lot * tpx[c] for c, _, lot in p0['tleg'])
                oth = 1 - leg
                oth_v = sim['tr_qty'][oth] * closes[sim['tr_hold'][oth]][D] if sim['tr_hold'][oth] != 'CASH' else 0
                cash_for = cash_now + (sim['tr_qty'][leg] * closes[cur_r][D] if cur_r != 'CASH' else 0)
                total = cash_for + base_v + oth_v
                qty = int(min(0.245 * total, cash_for) / closes[best_r][D] / 100) * 100
            else:
                qty = 0
            if best_r == cur_r:
                md += ['', f'🔄 明天调仓日: 第{leg + 1}份实盘持仓已是 {SHORT[cur_r]}, **不动**。']
            elif best_r != 'CASH' and qty == 0:
                md += ['', f'🔄 明天调仓日: 信号={SHORT[best_r]}, 但单份预算买不起一手(整手取整=0), **留现金不动**(与策略取整规则一致)。']
            else:
                md += ['', f'🔄 明天调仓日: 第{leg + 1}份实盘动作 **{SHORT.get(cur_r, "现金")} → {SHORT[best_r]}**:']
                if cur_r != 'CASH':
                    md.append(f'   - 9:15 卖出 {NAME[cur_r]} {sim["tr_qty"][leg]}股 @跌停价')
                if best_r != 'CASH':
                    md.append(f'   - 9:31 买入 {NAME[best_r]} **{qty}股**(≈{qty * closes[best_r][D]:.0f}元) @五档价')
    md += ['', '## 二、轮动腿(三策略共用同一信号)', '']
    if rot_head:
        md.append(f'1. {rot_head}')
        md += [f'   - {s_}' for s_ in rot_steps]
    else:
        eta = f'约{11 - nxt_tday}个交易日后(第11交易日)' if nxt_tday < 11 else '下月第1个交易日'
        md.append(f'1. 明天不是调仓日, 无动作; 下次调仓: {eta}')
    md.append(f'2. {prem_line}')
    md += ['3. 持仓与动量:', '',
           '| 仓位 | 应持有(稳态口径) | 自何时 |', '| --- | --- | --- |']
    for i in (0, 1):
        md.append(f'| 第{i + 1}份 | {NAME.get(hold[i], "?")} | {last_reb[i]} |')
    md += ['', '| 排名 | 标的 | 15日动量 |', '| --- | --- | --- |']
    for i, s_ in enumerate(rank):
        note = ' ⚠️溢价触闸' if (s_ == 'sh513100' and prem_block) else ''
        md.append(f'| {i + 1} | {NAME[s_]}{note} | {sc[s_]:+.1%} |')
    md += ['', '## 三、参考: 其余策略做T', '']
    for strat in STRATS[1:]:
        md.append(f'**{strat["name"]}**')
        md.append('')
        md += tleg_table(strat)
        md.append('')
    md += ['## 四、日历', '']
    cal = []
    if is_dca:
        cal.append('💰 明天周三: 转入定投 **1000 元**')
    if nxt_tday == 1:
        cal.append('🗓 明天月初: rebase做T批量(只上调), 新批量=int(总值×0.5×w×0.49÷现价÷100)×100')
    if nxt_tday not in (1, 11):
        cal.append(f'下个调仓日: 当月第11交易日(还差{11 - nxt_tday}个交易日)' if nxt_tday < 11
                   else '下个调仓日: 下月第1个交易日')
    md += [f'- {c_}' for c_ in (cal or ['无'])]
    body = '\n'.join(md)
    title = (('🔄调仓日 ' if nxt_tday in (1, 11) else '') + ('💰定投 ' if is_dca else '') +
             f'合体三策略 {nxt} 操盘单')

    key = os.environ.get('SERVERCHAN_KEY', '')
    if key:
        data = urllib.parse.urlencode({'title': title, 'desp': body}).encode()
        req = urllib.request.Request(f'https://sctapi.ftqq.com/{key}.send', data=data)
        print(urllib.request.urlopen(req, timeout=20).read().decode()[:200])
    print(title + '\n' + body)

if __name__ == '__main__':
    main()
