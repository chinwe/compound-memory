# compound-memory

本地多 Agent 共享记忆系统：记忆以 Markdown 文件落盘，随使用增值（复利），长期不用则衰减归档、可复活。

## Language

### 记忆与存储

**Memory（记忆）**：
一条被记录的知识单元，带类型、来源、置信度与使用历史，落盘为带 frontmatter 的 Markdown。
_Avoid_: 记录、entry、note

**Memory type（记忆类型）**：
四种之一——episode（经历）、fact（事实）、insight（洞察）、skill（技能）。类型决定检索权重、新近半衰期与归档 TTL，全系统只有一张类型规格表。
_Avoid_: category、kind

**Namespace（命名空间）**：
记忆的可见范围：`_shared` 全体 Agent 共享；`agent-*` 仅属主 Agent 可读写（读/反馈须带属主身份）。
_Avoid_: scope、bucket

**Local-first（本地优先）**：
约束对象是记忆系统服务（存储 / 检索 / 蒸馏的确定性部分）：不外发数据、不依赖外部服务。记忆内容流入调用方 Agent 的模型上下文是 Agent 侧既有的使用契约，不受此约束。
_Avoid_: 数据不出机（字面绝对化会连 search 动线一起禁掉）

**Confidence（置信度）**：
0~1 的数值，表示一条记忆的证据正确性：由带 outcome 的反馈事件折算——success 与跨 Agent 验证提高它，failure 降低它（地板 0.05），contradiction 冻结待队列裁决；usefulness 不在其中，由 uses 单独承载。
_Avoid_: score、weight（weight 专指类型权重）

**Validity window（有效期）**：
事实的生效区间标注：`valid_from` 是生效日期标注，不参与检索可见性（检索无 as-of 语义，未来才生效的事实照常可召回）；`valid_until` 当日仍有效、次日起退出检索与邻居召回（get 恒可读，不等于归档）。事实更替仍走 review 队列裁决——valid_until 只管检索可见性，不做自动失效改写。
_Avoid_: 过期/expire（口语动词，规范说法是「valid_until 已过」）、TTL（那是类型的归档寿命）

**Compounding（复利）**：
记忆系统随使用增值的机制：带结果的反馈强化置信度、双向关联在 get/search 时带出邻居、跨宿主验证（每宿主每记忆一次）额外加分。
_Avoid_: 加分、利息

**Independent evidence（独立证据）**：
来自不同宿主的直接使用证据：每宿主对每条记忆只计一次独立性加成，同宿主多会话不叠加；蒸馏产物的证据不构成对其源的独立验证（派生证据不回流，产物证据零起点）。
_Avoid_: 交叉验证、独立确认（宿主间可能共享同一错误假设，语义级独立性不可判定）

**Distillation（蒸馏）**：
把一批源记忆沉淀为更少、更高密度产物的过程。确定性段产出候选清单（含疑似重复标注），判断段由调用方 Agent 完成取舍；产物 links 溯源到源，源归档可复活。
_Avoid_: 压缩、summarize（那只是判断段的一个动作）

**Promotion（晋升）**：
记忆的洞见价值经蒸馏确认的形态：蒸馏产物（更高密度的新记忆）即晋升结果；源记忆类型终身不变，强化状态由 uses/confidence 表达。
_Avoid_: 升级、类型迁移（type 原地变更已禁）

**Review queue（冲突队列）**：
同 key 同类型的 fact/insight 内容冲突、或显式 contradiction 反馈登记时，等待人工复核的队列。
_Avoid_: conflict list

### 生命周期

**Decay（衰减）**：
长期未用且少用的记忆移入 archive 的过程。判据是"长期未用"，不是创建时间。
_Avoid_: 过期、expire

**Archive（归档）**：
衰减记忆的存放区。归档记忆不可检索、仍可 get，可复活——归档是可逆的。
_Avoid_: 删除、trash

**Revive（复活）**：
把归档记忆移回活动区，恢复可检索性。

**Forget（遗忘）**：
把记忆从活动库移除的终态治理动作：文件物理移出、一条 forget 提交留痕，内容仅存在于 git 历史；系统内不可逆，恢复是带外 git 运维。与 Archive 相对——归档是可逆的暂不检索，遗忘是永不再被系统携带。
_Avoid_: 删除（暗示无痕；遗忘有提交留痕）、tombstone（墓碑本体不留活动区，墓碑即那条 forget 提交）

**Recency reference（新近基准）**：
判断一条记忆"多新"的时间基准：`last_used` 优先，无则 `created`。落点是 `scoring.recency_age(mem, now)`——直接交出基准距 today 的天数，坏/缺日期交 `None`；基准选择只在这一处，ISO 解析降级共用 `scoring.age_days`，消费方只决定 `None` 的业务动作（排序记 0 分、衰减跳过）。不得各算各的。
_Avoid_: 参考时间、基准日期

### 检索

**Index（检索缓存）**：
token→路径的可重建缓存，只索引活动区记忆。不变量：活动记忆必被索引，归档记忆必不在索引；缓存丢失或损坏时自动重建，检索永远降级而不报错。向量索引（sqlite-vec）是它的姊妹缓存，守同一组不变量，依赖缺失时整体降级纯词面。
_Avoid_: 缓存、tokens.json（那只是它的落盘形态）

**Ranking（检索排序）**：
对候选记忆按相似度、置信度、新近度、类型权重合成单一分数并排序，产出搜索结果。搜索结果长什么样，由这里一处定义。双路（词面 + 向量）时以 RRF rank 融合为主序，先验仅做小幅 tie-break——先验不得翻过 rank 差。
_Avoid_: score、search（search 是整个动作）
