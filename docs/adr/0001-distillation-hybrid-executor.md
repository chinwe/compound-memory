# 蒸馏执行者采用混合架构：server 零 LLM，判断步后置到 agent

蒸馏提纯拆两段：确定性段（候选扫描、合并建议清单、归档动作）落在 compound-memory 自身代码、经 CLI 暴露；判断段（摘要 / 合并取舍）由调用方 Agent 在会话内完成，产出经既有 write/link 落库。被否决的全自动方案（launchd + 进程内调 LLM API）因打破 server 零 LLM 依赖并引入凭据管理而出局；纯 agent 侧方案则无法兑现 spec story 13 的"定时自动运行"——混合架构把它改写为"定时准备 + Agent 按需判断"（spec 措辞随实现票同步）。配套术语裁决见 CONTEXT.md 的 Local-first 条目：本地优先约束系统服务，不管 Agent 消费记忆时的上下文流转。

显式半自动落库（CLI `dream-apply`，#72）不把判断挪回 server：调用方必须先给出产物正文（`content` / `type` / `source_ids`），进程只把过写盘门槛的强信号——`merge_with`、`key_duplicates`，以及调用方点名的 `promotion_candidate`——编排进既有 `distill_apply`。`possible_dup_of` 只留在只读报告里。定时准备仍只跑只读 `dream`。无进程内 LLM，无 git revert 辅助命令。
