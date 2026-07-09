#!/usr/bin/env python3
"""生成静态监控页 docs/index.html — 由 GitHub Actions 每晚在 remind.py 之后运行, 经 GitHub Pages 发布.

内容: 策略TWR净值曲线(含换仓买卖点标记) + 当前状态(持仓/动量/溢价/闸门) + 最近事件表.
账务裁判权归聚宽模拟盘, 本页是驾驶舱: 曲线由公开行情逐日重算(TWR口径, 不含定投现金流).
多策略预留: 在 STRATEGIES 里加条目即可.
"""
import json, os, datetime
from remind import (fetch_qfq, fetch_nav_513100, fetch_raw_price,
                    ROT, TLEG, L, CASH_SCORE, NAME, CST, replay_rotation)

START = '2024-01-01'   # 曲线起点
FEE = 1.0              # bp 单边
CASH_DAILY = 0.015 / 243
PREM_TH = 0.08

def build_strategy(title, W):
    K = {c: fetch_qfq(c, 900) for c in set(ROT) | set(W)}
    OC = {c: {d: (o, cl) for d, o, cl in K[c]} for c in K}

    # --- 轮动腿(双份1/11, L15, 纳/金/债/现金) ---
    rdays = sorted(set.intersection(*[set(OC[c]) for c in ROT]))
    R = {}
    for s in ROT:
        R[s] = {}; pc = None
        for d in rdays:
            o, c = OC[s][d]
            if pc: R[s][d] = (o / pc - 1, c / o - 1, c / pc - 1)
            pc = c
    rdays = [d for d in rdays if all(d in R[s] for s in ROT)]
    tday, lastm, cnt = {}, None, 0
    for d in rdays:
        if d[:7] != lastm: lastm, cnt = d[:7], 0
        cnt += 1; tday[d] = cnt
    hist = {s: [] for s in ROT}
    hold = [None, None]
    rot_ret, events = {}, []
    for d in rdays:
        day = 0.0
        for leg, rb in ((0, 1), (1, 11)):
            cur = hold[leg]; tgt = cur
            if tday[d] == rb:
                sc = {s: hist[s][-1] / hist[s][-L] - 1 for s in ROT if len(hist[s]) >= L}
                if sc:
                    sc['CASH'] = CASH_SCORE
                    tgt = max(sc, key=sc.get)
            if cur is None:
                r = CASH_DAILY if tgt in (None, 'CASH') else (1 + R[tgt][d][1]) * (1 - FEE / 1e4) - 1
                hold[leg] = tgt
            elif tgt != cur:
                if d >= START:
                    events.append({'date': d, 'leg': leg + 1,
                                   'from': NAME.get(cur, cur), 'to': NAME.get(tgt, tgt)})
                if cur == 'CASH': r = (1 + R[tgt][d][1]) * (1 - FEE / 1e4) - 1
                elif tgt == 'CASH': r = (1 + R[cur][d][0]) * (1 - FEE / 1e4) * (1 + CASH_DAILY) - 1
                else: r = (1 + R[cur][d][0]) * (1 - FEE / 1e4) * (1 + R[tgt][d][1]) * (1 - FEE / 1e4) - 1
                hold[leg] = tgt
            else:
                r = CASH_DAILY if cur == 'CASH' else R[cur][d][2]
            day += 0.5 * r
        rot_ret[d] = day
        for s in ROT: hist[s].append(OC[s][d][1])

    # --- 做T腿(500/银/2000, 250日隔夜闸门月度更新) ---
    tdays = sorted(set.union(*[set(OC[s]) for s in W]))
    TRm = {}
    for s in W:
        TRm[s] = {}; pc = None
        for d in sorted(OC[s]):
            o, c = OC[s][d]
            if pc: TRm[s][d] = (o / pc - 1, c / o - 1, c / pc - 1)
            pc = c
    hov = {s: [] for s in W}
    act = {s: True for s in W}
    lastm = None
    t_ret = {}
    for d in tdays:
        if d[:7] != lastm:
            lastm = d[:7]
            for s in W:
                if len(hov[s]) >= 250:
                    act[s] = sum(hov[s][-250:]) / 250 * 1e4 < -2.0
        day = 0.0
        for s in W:
            if d not in TRm[s]: continue
            ov, it, tot = TRm[s][d]
            day += W[s] * (((1 + 0.5 * ov) * (1 + it) * (1 - 0.5 * FEE / 1e4) - 1) if act[s] else 0.5 * tot)
            hov[s].append(ov)
        t_ret[d] = day

    # --- 合体曲线 ---
    days = [d for d in tdays if d in rot_ret and d >= START]
    curve, v = [], 1.0
    peak, mdd = 1.0, 0.0
    for d in days:
        v *= 1 + 0.5 * t_ret[d] + 0.49 * rot_ret[d]
        peak = max(peak, v); mdd = min(mdd, v / peak - 1)
        curve.append(round(v, 5))
    ann = curve[-1] ** (243 / len(days)) - 1 if days else 0

    # --- 当前状态 ---
    hold2, last_reb, _ = replay_rotation({s: {d: OC[s][d][1] for d in rdays} for s in ROT}, rdays)
    sc = {s: OC[s][rdays[-1]][1] / OC[s][rdays[-L]][1] - 1 for s in ROT}
    sc['CASH'] = CASH_SCORE
    rank = sorted(sc, key=sc.get, reverse=True)
    nav = fetch_nav_513100()
    px = fetch_raw_price('sh513100') or OC['sh513100'][rdays[-1]][1]
    prem = (px / nav[1] - 1) if nav else None
    gate = {s: act[s] for s in W}
    return {
        'name': title,
        'asof': rdays[-1], 'ann': ann, 'mdd': mdd, 'cum': curve[-1] if curve else 1,
        'dates': days, 'curve': curve, 'events': events[-30:],
        'state': {
            'hold': [f'第{i+1}份: {NAME.get(hold2[i], "?")} ({last_reb[i]}起)' for i in (0, 1)],
            'rank': ' > '.join(f'{NAME[s].split(" ")[0]} {sc[s]:+.1%}' for s in rank),
            'prem': (f'{prem:+.1%}' + (' ⚠️超8%闸' if prem > PREM_TH else '')) if prem is not None else '未取到',
            'gate': ' '.join(f'{s[2:]}{"✅" if a else "⛔"}' for s, a in gate.items()),
        },
    }

STRATEGIES = [   # 以后加策略: 加一个 lambda 即可
    lambda: build_strategy('⭐ v6b 银行+中证2000做T + 纳金债现轮动 (实操)', {'sh512800': 0.5, 'sh563300': 0.5}),
    lambda: build_strategy('v6 银行+中证1000做T + 纳金债现轮动', {'sh512800': 0.5, 'sh512100': 0.5}),
    lambda: build_strategy('v5.1 中证500+银行+2000做T + 纳金债现轮动', {'sh510500': 0.5, 'sh512800': 0.3, 'sh563300': 0.2}),
]

HTML = '''<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>策略监控</title><style>
body{font-family:-apple-system,sans-serif;max-width:860px;margin:20px auto;padding:0 12px;background:#fafafa;color:#222}
.card{background:#fff;border:1px solid #e5e5e5;border-radius:10px;padding:16px;margin:14px 0}
h1{font-size:20px} h2{font-size:16px;margin:4px 0 10px}
.kpi{display:inline-block;margin-right:18px;font-size:14px}.kpi b{font-size:18px}
canvas{width:100%;height:260px}
table{width:100%;border-collapse:collapse;font-size:13px}
td,th{padding:5px 8px;border-bottom:1px solid #eee;text-align:left}
.muted{color:#888;font-size:12px}
</style></head><body>
<h1>📈 策略模拟监控 <span class="muted">数据截至 __ASOF__ · TWR口径不含定投 · 账务以聚宽模拟盘为准</span></h1>
__BODY__
<script>
function draw(id, dates, curve, events){
  const c = document.getElementById(id), ctx = c.getContext('2d');
  const W = c.width = c.clientWidth * 2, H = c.height = 520;
  const lo = Math.min(...curve), hi = Math.max(...curve), pad = 30;
  const x = i => pad + (W - 2 * pad) * i / (curve.length - 1);
  const y = v => H - pad - (H - 2 * pad) * (v - lo) / (hi - lo || 1);
  ctx.strokeStyle = '#2b7de9'; ctx.lineWidth = 3; ctx.beginPath();
  curve.forEach((v, i) => i ? ctx.lineTo(x(i), y(v)) : ctx.moveTo(x(i), y(v)));
  ctx.stroke();
  const di = Object.fromEntries(dates.map((d, i) => [d, i]));
  events.forEach(e => { const i = di[e.date]; if (i === undefined) return;
    ctx.fillStyle = '#e9612b'; ctx.beginPath(); ctx.arc(x(i), y(curve[i]), 7, 0, 7); ctx.fill(); });
  ctx.fillStyle = '#666'; ctx.font = '20px sans-serif';
  ctx.fillText(dates[0], pad, H - 6); ctx.fillText(dates[dates.length - 1], W - 150, H - 6);
  ctx.fillText(hi.toFixed(2), 4, y(hi) + 18); ctx.fillText(lo.toFixed(2), 4, y(lo));
}
__DRAWCALLS__
</script></body></html>'''

def render(strats):
    body, calls = [], []
    for i, s in enumerate(strats):
        ev_rows = ''.join(f'<tr><td>{e["date"]}</td><td>第{e["leg"]}份</td>'
                          f'<td>{e["from"]} → {e["to"]}</td></tr>' for e in reversed(s['events']))
        body.append(f'''<div class="card"><h2>{s["name"]}</h2>
<span class="kpi">起点以来 <b>{(s["cum"]-1)*100:+.1f}%</b></span>
<span class="kpi">年化 <b>{s["ann"]*100:+.1f}%</b></span>
<span class="kpi">最大回撤 <b>{s["mdd"]*100:.1f}%</b></span>
<canvas id="ch{i}"></canvas>
<p>🔄 {s["state"]["hold"][0]} | {s["state"]["hold"][1]}<br>
📊 动量榜: {s["state"]["rank"]}<br>
💹 纳指溢价: {s["state"]["prem"]} &nbsp; 🔁 做T闸门: {s["state"]["gate"]}</p>
<table><tr><th>日期</th><th>仓位</th><th>换仓(图中橙点)</th></tr>{ev_rows}</table></div>''')
        calls.append(f'draw("ch{i}", {json.dumps(s["dates"])}, {json.dumps(s["curve"])}, '
                     f'{json.dumps(s["events"], ensure_ascii=False)});')
    html = (HTML.replace('__ASOF__', strats[0]['asof'])
                .replace('__BODY__', '\n'.join(body))
                .replace('__DRAWCALLS__', '\n'.join(calls)))
    os.makedirs('docs', exist_ok=True)
    with open('docs/index.html', 'w') as f:
        f.write(html)
    print(f'docs/index.html 已生成 ({len(html)//1024}KB), 截至 {strats[0]["asof"]}')

if __name__ == '__main__':
    render([f() for f in STRATEGIES])
