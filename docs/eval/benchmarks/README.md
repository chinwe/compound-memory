# 必踩题

固定题在 [`must-hit.yaml`](must-hit.yaml)。八题，人来跑，没有打分脚本。协议、通过线、以及什么不算有效，都在 [`../README.md`](../README.md)。

每题只种自己的 `seed`，用一个新的 `COMPOUND_MEMORY_ROOT`。负向题不要和正向题种进同一个库。

`COMPOUND_MEMORY_AGENT_ID` 与 `seed.source` 保持一致：

```bash
export COMPOUND_MEMORY_ROOT="${TMPDIR:-/tmp}/compound-memory-eval-<id>"
export COMPOUND_MEMORY_AGENT_ID=<seed.source>
uv run compound-memory init
uv run compound-memory write "<seed.content>" <seed.type> <seed.source> \
  --ns _shared --key <seed.key>
```

条件 A 跳过 `write`。条件 B 在种入之后把 `prompt` 交给 agent，并要求先 `memory_search`、再按该题的 `feedback_outcome` 调用 `memory_feedback`。

跨宿主题 `cross-agent-test-clock` 的读取与反馈要换成 `reader_agent`，见上一级文档的命令。
