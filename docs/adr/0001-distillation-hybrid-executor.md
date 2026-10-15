# 蒸馏执行者采用混合架构：server 零 LLM，判断步后置到 agent

蒸馏提纯拆两段：确定性段（候选扫描、合并建议清单、归档动作）落在 compound-memory 自身代码、经 CLI 暴露；判断段（摘要 / 合并取舍）由调用方 Agent 在会话内完成，产出经既有 write/link 落库。被否决的全自动方案（launchd + 进程内调 LLM API）因打破 server 零 LLM 依赖并引入凭据管理而出局；纯 agent 侧方案则无法兑现 spec story 13 的"定时自动运行"——混合架构把它改写为"定时准备 + Agent 按需判断"（spec 措辞随实现票同步）。配套术语裁决见 CONTEXT.md 的 Local-first 条目：本地优先约束系统服务，不管 Agent 消费记忆时的上下文流转。

显式半自动落库（CLI `dream-apply`，#72）不把判断挪回 server：调用方必须先给出产物正文（`content` / `type` / `source_ids`），进程只把过写盘门槛的强信号——`merge_with`、`key_duplicates`，以及调用方点名的 `promotion_candidate`——编排进既有 `distill_apply`。`possible_dup_of` 只留在只读报告里。定时准备仍只跑只读 `dream`。无进程内 LLM，无 git revert 辅助命令。

只读报告上的建议动作（#75）取值只有 `merge`、`promote`、`review`。同 key 多版（含活性门外的 `key_duplicates`）标 `merge`，簇用 `source_ids` 列出；调用方若要综合跨 key 的多源，动作名同样是 `merge`（不另设 synthesize，不写 `supersedes:` frontmatter，溯源仍是产物已有的 `links`）。`possible_dup_of` 标 `review`，不进入 `dream-apply`。`promote` 仍须调用方显式点名并给出正文。类型晋升仍是新写更高 type + 源归档。教训语义（#78）同样只进只读报告：`lesson_role` 与 `avoid:` / `fixes:` / `contradicts:` 前缀供 `negative_salience`、`contradiction_pairs` 确定性扫描；不新增记忆类型，不放宽 dream-apply。借用教训语义，不搬因果引擎。
