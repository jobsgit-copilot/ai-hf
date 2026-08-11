---
name: magician-vcp-entry
description: "AI Berkshire skill: VCP 买点：波动收缩识别 + 中枢点突破. Source: skills/magician-vcp-entry.md."
---

## Codex adapter note

This skill is generated from `skills/magician-vcp-entry.md` so Claude Code and Codex users share one canonical workflow.

- Treat `$ARGUMENTS` as the user's request in the current Codex thread.
- When the source mentions Claude-only surfaces such as Task, Agent, WebSearch, Bash, Read, or Write, use the closest Codex capability available in this session: subagents when available, web search when needed, shell commands for local tools, and normal file edits for workspace files.
- Use shared project tools from `tools/` in this repository. Prefer running commands from the repository root with paths like `python3 tools/financial_rigor.py ...`; if the current thread starts outside the repo, locate the actual checkout path first instead of assuming a fixed home-directory path.
- Before starting research, run the `date` command to confirm today's date; treat it as the baseline for "latest" data and state the data cutoff date in the report header. Never assume the current date from training data.
- Preserve the research quality rules from `AGENTS.md`: cross-check financial data, use exact arithmetic tools for valuation/math, and clearly label uncertainty and source gaps.

# VCP 买点：波动收缩识别 + 中枢点突破

对 $ARGUMENTS 检测 VCP（波动收缩模式）结构，输出买点清单（买点/止损/目标）。本 skill 是 SEPA 漏斗的第三层，只做量价结构与买点，不做基本面与仓位。

## 适用场景

- 对趋势候选池（`magician-trend-screen` 输出、`magician-growth-fundamental` 通过）检测买点
- "XX 现在能不能买？结构完整吗？"

方法论来源：《纵横天下股市的奥秘》第 10 章（波动收缩规律）。

---

## 第一步：运行检测

```bash
python3 tools/vcp_detector.py detect 300308 --json      # 单只
python3 tools/vcp_detector.py scan 300308 600519 ...    # 批量
python3 tools/vcp_detector.py detect 300308 --end 20260201   # 历史回查（回测/复盘）
```

输出字段：中枢点(pivot)、收缩序列(depths)、技术足迹(footprint)、量能收缩(volume_dry)、状态(status)、阶段上下文(stage_context)、建议止损(stop_loss)。

## 第二步：解读结构

### 收缩序列（必须逐次递减）

- 基底内回调深度应**逐次收窄**，每次约为前次一半（容忍小幅波动）：如 25% → 15% → 8%。
- 通常 2-4 次收缩（原著：2-6 次）；首次收缩最深的 25-30% 属正常，末次收窄到 5-10% 为佳。
- **收缩加深（如 25→33→44）是派发而非 VCP**，直接否决。

### 技术足迹

- 格式：`{宽度}W {首段深度}/{末段深度} {次数}T`，如 `6W 32/6 3T` = 基底 6 周、波动从 32% 收窄到 6%、3 次收缩。
- 越紧的足迹（末段深度小、次数适中）越优。

### 量能萎缩（核心确认条件）

- 随收缩推进，回调日成交量应逐级萎缩（供给出清）；工具口径：末段收缩量能 ≤ 首段 60%。
- 带量数据可用时，**量能萎缩是核心增强条件**：2020-2026 日度回测中，同参数下期望 +0.9~1.7pp、胜率与"先达+20%"同步提升（`reports/magician-vcp-backtest-daily-20260811.md`）。
- 若工具提示"量能未随收缩萎缩"→ 降低优先级并在报告中注明；若数据缺失（缓存回退无成交量）→ 跳过该项并注明。

## 第三步：买点与风控

| 项目 | 规则 |
|------|------|
| 中枢点 | 基底最高点（pivot） |
| 买点 | 收盘**放量突破**中枢点；单日放量 ≥ 20 日均量 1.5 倍**不作硬性要求**（回测无增益），以突破后 2-3 日累计放量跟进确认为准 |
| 错过买点 | 等回踩不破中枢点再介入，不追高 |
| 止损 | 中枢点下方 7-10% 或基底最低点，**取价格更高者**（更近的止损） |
| 目标 | 按盈亏比 RR ≥ 2 设定（目标 = 止损风险 × 2） |
| 网球 vs 鸡蛋 | 回调缩量、快速反弹（网球）才持有；阴跌不止（鸡蛋）放弃 |

### 状态含义

- `breakout`：已放量突破 → 检查是否还能按计划上车（突破当日或次日）
- `setup`：接近中枢点（≤ 5% 以内）→ 挂条件单/尾盘确认
- `forming`：结构仍在形成 → 进观察池，等最后一次收缩完成
- `none`：无有效 VCP → 否决或观察

## 回测校准建议（Phase 4，2026-08）

基于 2020-2026 全市场回测（见 `reports/magician-vcp-backtest-20260811.md`）的经验性修正，**不改变原著方法论骨架**：

| 参数 | 建议 |
|---|---|
| 收缩次数 | 优先 ≥ 3 次（≥2 次只进观察池；量能萎缩成立时 ≥2 次可用，样本更大） |
| 追高限制 | 突破后收盘距中枢点 ≤ 15%，超过不追（等回踩） |
| 市场环境 | 不作为硬开关，作**仓位系数**：up_wide(指数>MA200且宽度≥60%)×1.0、up×0.8、down×0.5（down 只做量能萎缩+RS≥70 强信号，详见 sepa 第零步） |
| RS | 不作一票否决；≥70 加分；90 分位仅在量能萎缩同时成立时参考（日度复核为正，样本小） |
| 量能萎缩 | **必选确认**（数据可用时）：末段 ≤ 首段 60% |
| 突破放量 | 不单独过滤；突破后 2-3 日累计放量跟进确认 |

## 输出买入清单

```text
标的      足迹          中枢点    买点状态   止损     目标(RR≥2)   备注
300308.SZ 6W 32/6 3T   657.58   setup      592.00   792.00     突破日量能待确认
```

同时输出观察池：结构接近但未完成（收缩次数不足/最后一次未完成/量能未萎缩）的标的及等待条件。

## 硬性否决

- 收缩深度未递减或加深 → 否决（派发形态）
- 收缩次数 < 2 → 观察
- 末次收缩相对首次未显著收窄（> 70%）→ 观察
- 已处于第三/第四阶段（阶段上下文非 stage2）→ 由趋势层否决，此处复核并提示
- 无 VCP 结构时**禁止追高买入**
