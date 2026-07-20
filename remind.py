#!/usr/bin/env python3
"""合体策略族 每日操盘提醒 — 零依赖(纯标准库), GitHub Actions / 本地 cron 每晚运行.

覆盖三策略(轮动腿三者完全一致: 纳指/黄金/国债/现金, L15动量, 每月第1/11交易日双份错开, 纳指溢价>8%剔除):
  ⭐ v6b: 做T腿 = 银行512800×0.5 + 中证2000 563300×0.5   ← 用户实盘(银河证券, 4万)
     v6 : 做T腿 = 银行512800×0.5 + 中证1000 512100×0.5
     v5.1: 做T腿 = 中证500 510500×0.5 + 银行×0.3 + 2000×0.2
做T闸门: 250日隔夜均值<-2bp(逐标的, 月度更新), 三策略共用同一标的状态.
实盘账本(v6b): 从 live_since 起按实际事件重放 — 建仓/追加入金/每周三定投/底仓扩容日/
手动一手国债(两份轮动合持: 任一份信号离开国债时整手卖出, 另一份因买不起半手退现金)/
轮动调仓(整手买不起自动留现金). 估值按最新收盘价, 不含做T日内盈亏与利息.
有 SERVERCHAN_KEY 环境变量则推微信(Server酱, desp为Markdown), 否则打印.
口径与 JQ 各策略终版代码一致; 参数冻结, 勿改.
"""
import json, os, datetime, urllib.request, urllib.parse
from zoneinfo import ZoneInfo

# ======== 配置区(仅此处可改) ========
# 批量=int(资金×0.5×w×0.49/现价/100)×100; 月初rebase只上调。
STRATS = [
    {'key': 'v6b', 'name': 'v6b 银行+中证2000 做T', 'primary': True,
     'live_since': '2026-07-10',                    # 实盘建仓日(2万)
     'capital': 20000,
     'cash_in': [('2026-07-15', 10000),
                 ('2026-07-16', 10000)],            # 追加入金 → 共4万
     'dca': 1000,                                   # 每周三定投
     'dca_since': '2026-07-22',                     # 7-15未转定投, 从下周三起计
     'base_init': {'sh512800': 3200, 'sh563300': 1600},   # 7-10 实际建仓(2万口径)
     'base_topup': '2026-07-17',                    # 底仓扩容日: 早买新批量、尾卖旧批量
     'manual': [('2026-07-16', 'sh511010', 100, 140.805)],   # 手动一手国债(两份轮动合持)
     'tw': {'sh512800': 0.5, 'sh563300': 0.5},      # 做T腿内权重(rebase计算用)
     'tleg': [('sh512800', '512800 银行ETF', 6400),        # 4万口径批量(0.782价)
              ('sh563300', '563300 中证2000ETF', 3500)]},  # (1.413价)
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
    try:
        out = http_get(f'https://qt.gtimg.cn/q={code}')
        p = float(out.split('~')[3])
        return p if p > 0 else None
    except Exception:
        return None

def fetch_nav_513100():
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
    """实盘账本重放(估算). 返回 {started, cash, tr_hold, tr_qty, tr_cost, joint_bond,
    base, base_cost, bond_cost, t_pnl, vals[(日,总值,累计投入)]}
    总值 = 现金 + 持仓市值 + 做T累计盈亏(按协议每天执行的估算); 不含利息."""
    live = p0.get('live_since')
    if not live:
        return None
    cash = float(p0.get('capital', 0))
    invested = float(p0.get('capital', 0))
    tr_hold = ['CASH', 'CASH']
    tr_qty = [0, 0]
    tr_cost = [0.0, 0.0]
    joint_bond = False
    bond_cost = 0.0
    started = False
    t_pnl = 0.0
    vals = []
    dec = {}
    for d, leg, tgt in decisions:
        dec.setdefault(d, []).append((leg, tgt))
    lots_new = {c: lot for c, _, lot in p0['tleg']}
    base = dict(p0.get('base_init') or lots_new)
    base_cost = {c: 0.0 for c in base}
    topup = p0.get('base_topup', '')
    cash_in = {}
    for d, amt in p0.get('cash_in', []):
        cash_in[d] = cash_in.get(d, 0) + amt
    manual = {}
    for m in p0.get('manual', []):
        manual.setdefault(m[0], []).append((m[1], m[2], m[3] if len(m) > 3 else None))
    for d in dates:
        if d < live:
            continue
        mb = dict(base)   # 早盘时点的底仓(=当日做T批量)
        if not started:
            for c, lot in base.items():
                cash -= lot * opens[c][d]
                base_cost[c] += lot * opens[c][d]
        else:
            # 做T批次盈亏(协议口径: 每天开盘买批次/收盘卖批次)
            day_p = 0.0
            for c, q in mb.items():
                day_p += q * (closes[c][d] - opens[c][d])
                day_p -= 2 * max(0.1, q * opens[c][d] * 5e-5)
            t_pnl += day_p
        started = True
        cash += cash_in.get(d, 0)
        invested += cash_in.get(d, 0)
        if (d != live and datetime.date.fromisoformat(d).weekday() == 2
                and d >= p0.get('dca_since', live)):
            cash += p0.get('dca', 0)
            invested += p0.get('dca', 0)
        if topup and d == topup:
            for c, lot in lots_new.items():
                add = lot - base.get(c, 0)
                if add > 0:
                    cash -= add * opens[c][d]
                    base_cost[c] = base_cost.get(c, 0) + add * opens[c][d]
            base = dict(lots_new)
        for code, qty, px in manual.get(d, []):
            px = px if px else closes[code][d]
            cash -= qty * px
            if code == 'sh511010':
                tr_hold = ['sh511010', 'sh511010']
                tr_qty = [qty // 2, qty - qty // 2]
                tr_cost = [qty // 2 * px, (qty - qty // 2) * px]
                bond_cost = px
                joint_bond = True
        for leg, tgt in dec.get(d, []):
            cur = tr_hold[leg]
            if tgt == cur:
                continue
            if cur == 'sh511010' and joint_bond:
                cash += (tr_qty[0] + tr_qty[1]) * opens[cur][d]
                tr_hold = ['CASH', 'CASH']
                tr_qty = [0, 0]
                tr_cost = [0.0, 0.0]
                joint_bond = False
            elif cur != 'CASH':
                cash += tr_qty[leg] * opens[cur][d]
                tr_qty[leg] = 0
                tr_cost[leg] = 0.0
                tr_hold[leg] = 'CASH'
            if tgt != 'CASH':
                base_v = sum(lot * closes[c][d] for c, lot in base.items())
                oth = 1 - leg
                oth_v = tr_qty[oth] * closes[tr_hold[oth]][d] if tr_hold[oth] != 'CASH' else 0
                total = cash + base_v + oth_v
                qty = int(min(0.245 * total, cash) / opens[tgt][d] / 100) * 100
                if qty > 0:
                    cash -= qty * opens[tgt][d]
                    tr_hold[leg] = tgt
                    tr_qty[leg] = qty
                    tr_cost[leg] = qty * opens[tgt][d]
        pos_v = sum(q * closes[c][d] for c, q in base.items())
        for i in (0, 1):
            if tr_hold[i] != 'CASH':
                pos_v += tr_qty[i] * closes[tr_hold[i]][d]
        vals.append((d, cash + pos_v + t_pnl, invested))
    return {'started': started, 'cash': cash, 'tr_hold': tr_hold, 'tr_qty': tr_qty,
            'tr_cost': tr_cost, 'joint_bond': joint_bond, 'base': base,
            'base_cost': base_cost, 'bond_cost': bond_cost, 't_pnl': t_pnl, 'vals': vals}

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

    sc = {s: closes[s][rdates[-1]] / closes[s][rdates[-L]] - 1 for s in ROT}
    sc['CASH'] = CASH_SCORE
    rank = sorted(sc, key=sc.get, reverse=True)

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

    gate, tpx = {}, {}
    # 闸门口径=策略的月初锁定: 用 nxt 所在月首日前的数据; 若明天是月初(重设闸门日)则用全量
    g_cut = '9999' if nxt_tday == 1 else f'{nxt.year:04d}-{nxt.month:02d}-01'
    for code in TLEG:
        rows = fetch_qfq(code, 320)
        ov = [rows[i][1] / rows[i - 1][2] - 1 for i in range(1, len(rows)) if rows[i][0] < g_cut]
        m = sum(ov[-250:]) / min(250, len(ov)) * 1e4
        gate[code] = (m, m < -2.0)
        opens[code] = {d: o for d, o, _ in rows}
        closes[code] = {d: cl for d, _, cl in rows}
        tpx[code] = rows[-1][2]
    tpx['sh511010'] = closes['sh511010'][D]

    p0 = STRATS[0]
    sim = simulate_live(p0, opens, closes, rdates, decisions)
    live = p0.get('live_since', '')
    topup = p0.get('base_topup', '')
    is_live_day = live == str(nxt)
    is_topup_day = topup == str(nxt)
    pre_live = (not is_live_day) and (sim is not None and not sim['started']) and live > str(today)
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
        parts.append(f'⭐{p0["key"]}未开跑({live}建仓)')
    elif is_live_day:
        parts.append(f'⭐{p0["key"]} **实盘建仓日: 只买底仓, 今天不卖**')
    elif is_topup_day:
        parts.append('⭐v6b **底仓扩容日: 早买新批量/尾卖旧批量**')
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

    def pos_str(intraday=False):
        """实盘持仓描述(估值=最新收盘). intraday=早盘批次买入后"""
        items = []
        for c, name_, lot in p0['tleg']:
            q = sim['base'].get(c, 0) + (lot if intraday else 0)
            items.append(f'{name_.split(" ")[1]}{q}股(≈{q * tpx[c]:.0f}元)')
        if sim['joint_bond']:
            q = sim['tr_qty'][0] + sim['tr_qty'][1]
            items.append(f'国债ETF{q}股·两份合持(≈{q * tpx["sh511010"]:.0f}元)')
        else:
            for i in (0, 1):
                if sim['tr_hold'][i] != 'CASH':
                    c = sim['tr_hold'][i]
                    q = sim['tr_qty'][i]
                    items.append(f'{SHORT[c]}{q}股(≈{q * closes[c][D]:.0f}元)')
        return ' + '.join(items) if items else '无'

    md = [summary, '', f'## ⭐ 一、{p0["name"]}(你的实操策略, 4万)', '']
    if sim and sim['started'] and sim['vals']:
        d_, val, inv = sim['vals'][-1]
        day_line = ''
        if len(sim['vals']) >= 2:
            pv, pinv = sim['vals'][-2][1], sim['vals'][-2][2]
            dchg = val - pv - (inv - pinv)
            day_line = f' · 当日({d_}) **{dchg:+.0f}元 ({dchg / pv:+.2%})**'
        md += [f'**账户快照(估)**: 总值≈**{val:.0f}元** / 投入{inv:.0f}元 · '
               f'累计 **{val - inv:+.0f}元 ({(val - inv) / inv:+.2%})**' + day_line, '',
               '| 持仓 | 数量 | 成本(估) | 现价 | 市值 | 盈亏 |', '| --- | --- | --- | --- | --- | --- |']
        for c, name_, lot in p0['tleg']:
            q = sim['base'].get(c, 0)
            avg = sim['base_cost'].get(c, 0) / q if q else 0
            mv = q * tpx[c]
            pnl = mv - sim['base_cost'].get(c, 0)
            md.append(f'| {name_.split(" ")[1]} | {q} | {avg:.3f} | {tpx[c]:.3f} | {mv:.0f} | {pnl:+.0f} ({pnl / sim["base_cost"][c]:+.1%}) |')
        if sim['joint_bond']:
            q = sim['tr_qty'][0] + sim['tr_qty'][1]
            cb = sim['bond_cost']
            px_ = closes['sh511010'][D]
            md.append(f'| 国债ETF(轮动合持) | {q} | {cb:.3f} | {px_:.3f} | {q * px_:.0f} | {q * (px_ - cb):+.0f} |')
        else:
            for i in (0, 1):
                if sim['tr_hold'][i] != 'CASH':
                    c = sim['tr_hold'][i]
                    q = sim['tr_qty'][i]
                    avg = sim['tr_cost'][i] / q if q else 0
                    px_ = closes[c][D]
                    md.append(f'| {SHORT[c]}(轮动{i + 1}) | {q} | {avg:.3f} | {px_:.3f} | {q * px_:.0f} | {q * px_ - sim["tr_cost"][i]:+.0f} |')
        md.append(f'| 现金(含逆回购) | — | — | — | {sim["cash"]:.0f} | — |')
        md.append(f'| 做T累计净贡献(协议口径) | — | — | — | {sim["t_pnl"]:+.0f} | — |')
        md += ['', '📏 口径: 成本=建仓/扩容均价(不含做T摊薄), 不含利息; **App实际总资产−本表总值≈执行偏差**, 长期差>200元请发我校准。', '']
    batch_cost = sum(lot * tpx[c] for c, _, lot in p0['tleg'])
    cash_now = sim['cash'] if sim and sim['started'] else 0
    if pre_live:
        md += [f'**准备期**: {live} 建仓, 无操作。']
    elif is_live_day:
        md += ['**实盘建仓日**: 只买入底仓, 不设卖单。', '']
        md += ['| 标的 | 底仓数量 | 委托 |', '| --- | --- | --- |']
        for code, name_, lot in p0['tleg']:
            md.append(f'| {name_} | **{lot}股** | 9:15 @涨停价 |')
    elif is_topup_day:
        old = sim['base']
        md += ['**明天是底仓扩容日**(资金已升至4万): 早上按**新批量**买入, 尾盘按**旧批量**卖出,'
               ' 收盘后底仓自动升级; 后天起每日买卖均为新批量。', '',
               '| 标的 | 9:15买(新批量) | 14:58卖(旧批量) | 闸门 |', '| --- | --- | --- | --- |']
        for code, name_, lot in p0['tleg']:
            m, a = gate[code]
            st = '✅' if a else '⛔停做T,今日只卖旧批'
            md.append(f'| {name_} | **{lot}股**@涨停价 | **{old.get(code, 0)}股**@跌停价 | {st} {m:+.1f}bp |')
        buy_cost = batch_cost
        end_cash = cash_now - sum((lot - old.get(c, 0)) * tpx[c] for c, _, lot in p0['tleg'])
        md += ['', '**账户预演(估算价=最新收盘)**', '',
               '| 时点 | 持仓 | 现金(约) |', '| --- | --- | --- |',
               f'| 开盘前 | {pos_str()} | ≈{cash_now:.0f}元 |',
               f'| 日内(9:15买入后) | {pos_str(intraday=True)} | ≈{cash_now - buy_cost:.0f}元(挂单需可用≥{buy_cost * 1.1:.0f}) |',
               f'| 收盘(卖旧批后) | 底仓升级为新批量+国债 | ≈{end_cash:.0f}元 ± 当日做T盈亏 |']
    else:
        md += tleg_table(p0)
        md += ['', '**账户预演(估算价=最新收盘, 现金不含做T盈亏/利息)**', '',
               '| 时点 | 持仓 | 现金(约) |', '| --- | --- | --- |',
               f'| 开盘前 | {pos_str()} | ≈{cash_now:.0f}元 |',
               f'| 日内(9:15批次买入后) | {pos_str(intraday=True)} | ≈{cash_now - batch_cost:.0f}元(9:15挂单需可用≥{batch_cost * 1.1:.0f}) |',
               f'| 收盘(14:58批次卖出后) | {pos_str()} | ≈{cash_now:.0f}元 ± 当日做T盈亏 |']
        if is_dca:
            md.append(f'| 定投到账后 | 不变 | ≈{cash_now + p0.get("dca", 0):.0f}元 |')
        if cash_now < batch_cost * 1.1:
            md += ['', f'⚠️ **资金偏紧**: 可用≈{cash_now:.0f} < 冻结需求≈{batch_cost * 1.1:.0f}, 明早买单请每只各减100股。']
        if nxt_tday in (1, 11) and sim and sim['started']:
            leg = 0 if nxt_tday == 1 else 1
            cur_r = sim['tr_hold'][leg]
            sc2 = dict(sc)
            if prem_block and 'sh513100' in sc2:
                del sc2['sh513100']
            best_r = max(sc2, key=sc2.get)
            if best_r == cur_r:
                md += ['', f'🔄 明天调仓日: 第{leg + 1}份实盘持仓已是 {SHORT[cur_r]}, **不动**。']
            elif cur_r == 'sh511010' and sim['joint_bond']:
                qj = sim['tr_qty'][0] + sim['tr_qty'][1]
                md += ['', f'🔄 明天调仓日: 第{leg + 1}份信号离开国债 → **卖出国债ETF整手{qj}股** 9:15@跌停价(两份合持解除):']
                if best_r != 'CASH':
                    md.append(f'   - 第{leg + 1}份 9:31 买入 {NAME[best_r]}, 数量=约24.5%总值÷现价 取整百')
                md.append('   - 另一份: 单份买不起整手国债, **留现金**(做逆回购)')
            else:
                bud = 0.245 * (cash_now + sum(l * tpx[c] for c, _, l in p0['tleg']))
                qty = int(min(bud, cash_now) / closes[best_r][D] / 100) * 100 if best_r != 'CASH' else 0
                if best_r != 'CASH' and qty == 0:
                    md += ['', f'🔄 明天调仓日: 信号={SHORT[best_r]}, 但单份预算买不起整手, **留现金不动**。']
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
        rnote = f'(总值不含备用金{p0.get("reserve", 0):.0f}元)' if p0.get('reserve') else ''
        cal.append(f'🗓 明天月初: rebase做T批量(只上调), 新批量=int(总值×0.5×w×0.49÷现价÷100)×100{rnote}')
    if nxt_tday not in (1, 11):
        cal.append(f'下个调仓日: 当月第11交易日(还差{11 - nxt_tday}个交易日)' if nxt_tday < 11
                   else '下个调仓日: 下月第1个交易日')
    md += [f'- {c_}' for c_ in (cal or ['无'])]
    body = '\n'.join(md)
    title = (('🔄调仓日 ' if nxt_tday in (1, 11) else '') + ('💰定投 ' if is_dca else '') +
             ('🧱扩容日 ' if is_topup_day else '') + f'合体三策略 {nxt} 操盘单')

    key = os.environ.get('SERVERCHAN_KEY', '')
    if key:
        data = urllib.parse.urlencode({'title': title, 'desp': body}).encode()
        req = urllib.request.Request(f'https://sctapi.ftqq.com/{key}.send', data=data)
        print(urllib.request.urlopen(req, timeout=20).read().decode()[:200])
    print(title + '\n' + body)

if __name__ == '__main__':
    main()
