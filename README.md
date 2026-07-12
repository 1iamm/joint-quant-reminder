# 合体v5.1 每日微信操盘提醒

零成本架构: **GitHub Actions(免费定时) → remind.py(信号计算,纯标准库) → Server酱(免费推微信)**。
不需要任何服务器。聚宽模拟盘不参与当日信号(延时盘凌晨3点才重放昨天,晚一天),只用作每周对账基准。

## 一次性配置(约10分钟)

1. **Server酱**: 微信扫码登录 https://sct.ftqq.com → 复制 SendKey → 关注「方糖」服务号。免费版 5条/天(本系统每天只发1条)。
2. **GitHub**: 仓库 https://github.com/1iamm/joint-quant-reminder (代码已推送)。
3. 添加 SendKey(本机执行,不要把 Key 发给任何人/任何会话):
   ```bash
   gh secret set SERVERCHAN_KEY -R 1iamm/joint-quant-reminder
   # 回车后粘贴 SendKey
   ```
4. 测试: `gh workflow run remind.yml -R 1iamm/joint-quant-reminder`,微信应收到消息。
5. **监控页**(需要仓库为 Public,免费版 Pages 不支持私有仓库):
   Settings → Pages → Source 选 `Deploy from a branch`,分支 `main`、目录 `/docs`。
   之后随时打开 `https://1iamm.github.io/joint-quant-reminder/`。

此后每周日~周四晚 18:37(北京时间)自动推送次日操盘单,并同步更新监控页。

## 消息内容

- 明天是否调仓日(每月第1/11交易日);是则给出 卖旧@9:15跌停价 / 买新@9:31五档价 指令
- 两份轮动仓位当前应持标的 + L15动量榜 + 纳指溢价(>8%剔除,7~9%提示复核)
- 做T六张条件单数量核对 + 250日隔夜闸门状态
- 周三定投提醒;月初 rebase 提醒

## 维护

- **实盘入金后**改 `remind.py` 顶部 `LOTS`(做T批量),公式在注释里;之后每月初按提醒 rebase。
- 参数(L15/8%闸/1与11调仓日/闸门-2bp)已冻结,勿改——历次变体测试全部否决,见研究档案。
- GitHub Actions cron 高峰期可能延迟几分钟到半小时,属正常。
- 每周可让 Claude 对账一次: 模拟盘凌晨重放的成交 vs 本脚本信号,应一致。
