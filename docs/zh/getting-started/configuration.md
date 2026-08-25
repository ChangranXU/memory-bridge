---
description: memory-bridge 的所有共享配置键。
---

# 配置参考

所有记忆设置位于 `agent.memory.*` 的 `MemoryConfig` 中。未知键会导致验证失败（`extra="forbid"`），因此拼写错误会立即被发现。集成通过继承 `MemoryConfig` 添加自己的字段。

## 核心设置

| 键 | 类型 | 默认值 | 描述 |
|---|---|---|---|
| `enabled` | `bool` | `false` | 主开关。`false` 时智能体是一个空操作包装器——与基线逐字节一致。 |
| `scope` | `"run" \| "instance"` | `"run"` | `run`：存储在 run root 的实例间共享。`instance`：每个任务使用新存储。 |
| `user_id` | `str` | `"minisweagent"` | 检索隔离身份。不能为空。 |
| `output_dir` | `str` | `""` | 每实例的产物根目录。`enabled=true` 时**必须设置**。 |
| `strict` | `bool` | `false` | `true`：后端错误抛入智能体循环（用于调试）。`false`：失效封闭。 |

## 录制

| 键 | 类型 | 默认值 | 描述 |
|---|---|---|---|
| `max_message_chars` | `int` | `4000` | 每条消息的录制上限。截断标记计入总数。 |

## 提取

| 键 | 类型 | 默认值 | 描述 |
|---|---|---|---|
| `extract_every_n_steps` | `int` | `10` | 提取频率。`0` 仅在最终刷新时提取。 |
| `extract_max_consecutive_errors` | `int` | `3` | 提取熔断器。`0` 禁用熔断。 |
| `extraction_guidelines` | `str` | `""` | 提取策略文本。`""` 使用共享默认值；非空值整体替换默认值。 |

{% hint style="info" %}
提取指引通过集成的原生通道传达。CURE 集成将其附加到 `MEMORY_POLICY_PROMPT`；mem0 将其作为 add 端点的 `custom_instructions` 发送。引擎不接受自定义提示规则的集成会忽略此字段（tencentdb 后端未声明该通道：其存储式提示路由按 scope 键控，且提取是异步的，每 episode 的上下文无法随之搭载）。
{% endhint %}

## 召回

| 键 | 类型 | 默认值 | 描述 |
|---|---|---|---|
| `inject_recall` | `bool` | `true` | 召回注入主开关。 |
| `max_memories` | `int` | `10` | 交付记忆行数的上限。 |
| `max_total_recall_chars` | `int` | `2000` | 渲染记忆行的总预算（header 不计入）；0 = 关闭。按排名填充执行，剩余预算 ≥ 40 字符时截断填充。 |
| `max_chars_per_memory` | `int` | `0` | 单条渲染记忆行（内容 + 溯源后缀）的上限，含截断后缀；0 = 关闭（原生默认值）。 |
| `search_timeout` | `float` | `10.0` | 单次原生搜索调用的超时（秒）。仅对网络搜索有效。 |
| `recall_min_score` | `float \| None` | `None` | 相关度下限。低于此分数的命中在任何数量限制前被丢弃。`None` 禁用。分数尺度由集成定义。 |

## 查询改写

| 键 | 类型 | 默认值 | 描述 |
|---|---|---|---|
| `rewrite_every_n_steps` | `int` | `0` | 查询改写频率。`0` 禁用改写（查询保持为任务文本）。 |
| `rewrite_max_consecutive_errors` | `int` | `3` | 改写熔断器。`0` 禁用熔断。 |
| `rewrite_model` | `str` | `""` | 改写器模型名。回退到 `MEMORY_QUERY_MODEL` 环境变量。 |
| `rewrite_base_url` | `str` | `""` | 改写器 base URL。回退到 `MEMORY_QUERY_MODEL_URL` 环境变量。 |
| `rewrite_api_key` | `str` | `""` | 改写器 API key。回退到 `MEMORY_QUERY_API_KEY` 环境变量。 |
| `rewrite_timeout` | `float` | `20.0` | 每次改写的 HTTP 超时（秒）。 |
| `rewrite_max_tokens` | `int` | `1600` | 改写器的最大完成 token 数。较大的默认值适配推理风格模型。 |

## 标注

| 键 | 类型 | 默认值 | 描述 |
|---|---|---|---|
| `annotate` | `bool` | `true` | 轨迹标注主开关。 |
| `annotate_main_url` | `str` | `""` | MAIN lane 的显式 annotate 端点。回退到环境变量，然后自动推导。 |
| `annotate_memory_url` | `str` | `""` | memory lane 的显式 annotate 端点。回退到环境变量，然后自动推导。 |
| `annotate_timeout` | `float` | `0.5` | 每次尝试的 HTTP 超时。 |
| `annotate_retries` | `int` | `1` | 仅对连接失败和 5xx 重试。 |
| `annotate_max_consecutive_errors` | `int` | `3` | 标注熔断器。 |

## 环境变量

bundle 根目录的提供商 `.env` 提供主要模型连接：

```dotenv
MODEL=deepseek-v4-flash
API_KEY=sk-...
BASE_URL=https://api.deepseek.com
API=openai-chat
```

### 可选环境变量

| 变量 | 用途 |
|---|---|
| `QUERY_MODEL` | 召回查询改写器模型（默认为 `MODEL`） |
| `QUERY_API_KEY` | 改写器 API key（默认为 `API_KEY`） |
| `QUERY_API` | 改写器 API 类型（默认为 `API`） |
| `EXTRACT_MODEL` | CURE 提取模型。仅作后端回退：记忆臂驱动器会基于 EXTRACT 代理 lane 按实例覆盖全部三个 `EXTRACT_*` 值，且 `agent.memory.extract_*` 配置字段优先于这些环境变量。 |
| `EXTRACT_BASE_URL` | CURE 提取端点（回退语义同 `EXTRACT_MODEL`）。 |
| `EXTRACT_API_KEY` | CURE 提取 API key（回退语义同 `EXTRACT_MODEL`）。 |
| `EMBEDDING_MODEL` | tencentdb embedding 模型。与 `EMBEDDING_API_KEY`、`EMBEDDING_BASE_URL` 和 `EMBEDDING_DIMENSIONS` 组成全有或全无的四元组——记忆臂驱动器拒绝部分设置（上游会将其静默禁用）。 |
| `EMBEDDING_API_KEY` | tencentdb embedding API key（全有或全无四元组的一部分——见 `EMBEDDING_MODEL`）。 |
| `EMBEDDING_BASE_URL` | tencentdb embedding 端点（全有或全无四元组的一部分——见 `EMBEDDING_MODEL`）。 |
| `EMBEDDING_DIMENSIONS` | tencentdb embedding 维度（全有或全无四元组的一部分——见 `EMBEDDING_MODEL`）。 |
| `MEM0_API_KEY` | mem0 Platform API key（在 `integration/mem0/.env` 中） |

{% hint style="warning" %}
凭据保存在 pydantic 字段中，标记为 `exclude=True, repr=False`。只有脱敏后的 URL 才会出现在产物和日志中——用户信息、查询字符串和片段被移除，轨迹 ID 被替换为其 16 个十六进制字符的 SHA-256 哈希前缀。
{% endhint %}
