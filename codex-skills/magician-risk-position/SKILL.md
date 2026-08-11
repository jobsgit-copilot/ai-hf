---
name: magician-risk-position
description: "AI Berkshire skill: 仓位与风控：单笔风险、组合集中度、加仓规则. Source: skills/magician-risk-position.md."
---

## Codex adapter note

This skill is generated from `skills/magician-risk-position.md` so Claude Code and Codex users share one canonical workflow.

- Treat `$ARGUMENTS` as the user's request in the current Codex thread.
- When the source mentions Claude-only surfaces such as Task, Agent, WebSearch, Bash, Read, or Write, use the closest Codex capability available in this session: subagents when available, web search when needed, shell commands for local tools, and normal file edits for workspace files.
- Use shared project tools from `tools/` in this repository. Prefer running commands from the repository root with paths like `python3 tools/financial_rigor.py ...`; if the current thread starts outside the repo, locate the actual checkout path first instead of assuming a fixed home-directory path.
- Before starting research, run the `date` command to confirm today's date; treat it as the baseline for "latest" data and state the data cutoff date in the report header. Never assume the current date from training data.
- Preserve the research quality rules from `AGENTS.md`: cross-check financial data, use exact arithmetic tools for valuation/math, and clearly label uncertainty and source gaps.

# 仓位与风控：单笔风险、组合集中度、加仓规则

对 $ARGUMENTS 计算仓位、检查组合约束、制定加仓/换仓规则。本 skill 是 SEPA 漏斗的第四层，只做资金与风险管理。

## 适用场景

- 交易计划卡已生成，需要确定"买多少"
- 组合已有持仓，需要检查集中度与加仓纪律

方法论来源：《纵横天下股市的奥秘》第 12-13 章、《像冠军一样思考和交易》第 2/8 章。

---

## 第一步：确认输入

- 账户总资金、可用资金
- 现有持仓清单（标的、成本、市值、占比）
- 待买入标的的买价与止损价（来自 `magician-vcp-entry`）

## 第二步：仓位公式

```
单笔风险金额 = 账户总资金 × 风险%
股数 = 单笔风险金额 ÷ (买价 − 止损价)
买入金额 = 股数 × 买价
```

| 参数 | 默认值 | 范围 |
|------|--------|------|
| 单笔风险 | 1.5% | 1.25% – 2.5% |
| 最大止损宽度 | 10% | 一般 5-6% 触发 |
| 目标盈亏比 RR | ≥ 2:1 | 低于 2 放弃 |

## 第三步：组合约束（硬性）

| 规则 | 限制 |
|------|------|
| 持仓数量 | 4-8 只；最优 4-5 只 |
| 单只初始占比 | ≤ 25% |
| 同板块上限 | ≤ 2 只 |
| 试探仓 | 目标仓位的 1/3 – 1/2，确认后加至目标 |

任一约束被突破且无换出对象 → 该笔放弃（交给 `magician-sepa` 硬性否决）。

## 第四步：加仓与换仓规则

- **永不摊低成本**：持仓下跌不补仓，避免把"截断亏损"变成"死扛"。
- **加仓只用盈利**：股价按预期上涨且结构延续（如站稳 50 日均线）才加，加仓后总风险不超限。
- **二换一原则**：要买更强的标的，先卖最弱的持仓，保持组合数量与集中度。
- **50/80 法则**：盈利 50% 后保护止损上移锁定利润；回吐超 80% 前必须处理（详见 `magician-sell-rule`）。

## 第五步：期望值校验（可选）

用交易日志（`magician-trade-journal`）的历史统计校准参数：

```
期望值 E = 胜率 × 平均盈利 − 败率 × 平均亏损
```

E 不为正时，降低单笔风险或提高选股标准，而不是提高仓位。

## 输出仓位计算表

```text
标的      买价     止损     单笔风险%  风险金额    股数    买入金额  占比   约束检查
300308.SZ 657.58   592.00   1.5%      15,000     2,286   1,503,000  22%   通过
```

同时输出组合快照：当前持仓数量/板块分布/总风险敞口，以及"换出候选"（最弱持仓）建议。

## 纪律要求

- 仓位必须提前按公式计算，盘中不临时加码。
- A 股 T+1：当日买入不可卖，止损预案要包含次日执行计划；跌停无法卖出时按 `magician-sell-rule` 的应急预案处理。
