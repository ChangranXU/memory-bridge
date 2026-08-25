# CURE 集成

**基于 [CURE 记忆系统](https://github.com/staymylove/CURE_memory_system) 的自动提取记忆,
面向编程智能体。目前已接入
[mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent) 用于 SWE-bench 评测。**

[English](README.md) | [简体中文](README.zh-CN.md)

完整的 `CUREMemorySystem` 生命周期在宿主侧与智能体循环并行运行。CURE 的**提取
LLM**——而非基准模型——是唯一的记忆决策者。基准模型只能看到原生的 `bash` 工具;
记忆以瞬态召回注入的方式进入每个回合,以被录制的消息形式离开,由提取 LLM 后续
处理。

## 回合生命周期

1. 打开 CURE 存储并以唯一回合 id 启动会话。
2. 录制每条轨迹消息（规范化、截断）。
3. 每 N 步（默认 10）及回合结束时,将新消息发送给提取 LLM 做候选/删除/拒绝
   决策。熔断器限制死端点的开销;最终冲刷始终运行。
4. 每次模型查询前,将审批通过的记忆注入为**瞬态** user 消息（模型可见,但永不
   持久化）。
5. `finalize()` 写入 `memory.json` 并关闭 SQLite。一切失效封闭;`strict: true`
   将容纳转为抛出。

## 适用性分层（`scope="run"`）

每条记忆的层在提取时由决策 LLM 的 `scope` 字段一次性确定,此后不再重分类:

- **仓库绑定**（`scope="project"`）——只适用于一个仓库的事实:文件路径、模块
  布局、仓库特定命令、本代码库的 API 或依赖约束。以 `project_id =
  <owner>__<repo>`（由实例 id 派生）存储,仅在该仓库的回合内可检索。
- **通用**（`scope="user"`）——跨仓库仍然成立的经验:调试方法、工具用法、通用
  工作流模式。以 `project_id = NULL` 存储,对本次运行的每个回合可见。

一个回合召回到的是本仓库的行加上所有通用行,永远不会是其他仓库的行。提取器
失效封闭（缺失或畸形的 scope 落为仓库绑定）,取代（supersede）在两个方向上都不
跨层（同 type+key 的通用候选不会取代仓库绑定行,仓库绑定候选也不会取代共享的
通用行——通用层对整个运行共享,因此两者共存,仓库绑定行在本仓库的召回中覆盖
通用行）,删除默认停留在会话自身所在层（只有显式携带 `scope: "user"` 才会触及
共享的通用行;终止状态的行永远不会被重复匹配,一次逻辑删除只计一次）,且召回行
标注所在层
（`- [workflow:repo] ...` / `- [fact:general] ...`）。`scope="instance"` 保持
逐实例隔离,行为不变。

## 架构

```text
python -m cure_memory_bridge.run.swebench    （薄运行器，CLI 与原生 swebench 一致）
        | 将 ProgressTrackingAgent 替换为 CureMemoryAgent
        v
CureMemoryAgent(MemoryAgent)                 # shared_bridge 的通用钩子外壳
        | 拥有（每回合一个）
        v
CureMemoryBackend（宿主侧，失效封闭）
        | CUREMemorySystem (SQLite) + ChatGPTMemoryDecisionClient (EXTRACT_*)
        v
memory.json + cure_memory.sqlite3
```

**智能体**挂钩生命周期节点,**后端**管理 CURE 的完整生命周期,**模型**保持原生。
通用组件（智能体外壳、配置基类、标注传输、端点契约）位于
[`shared-bridge/`](../../shared-bridge/README.zh-CN.md);本包将它们绑定到 CURE。
`CureMemoryEndpoint` 将 `CUREMemorySystem` 适配到共享的
`add`/`search`/`update`/`delete` 契约。

## 开关控制

| 状态 | 调用方式 | 行为 |
|---|---|---|
| **OFF**（基线） | `utils/run-predictions.sh` | 原生运行器,不导入任何桥接代码。 |
| **ON**（记忆臂） | `python -m cure_memory_bridge.run.swebench` + 配置叠加 + `enabled=true` | 相同模型/工具/模板,外加宿主侧记忆和瞬态注入。 |
| **调试**（`enabled=false`） | 桥接运行器 + 叠加,但 `enabled=false` | 无后端;轨迹与基线逐字节一致（仅 `info.config` 元数据不同）。 |

A/B 对比 = OFF vs. ON。不覆盖 `model.model_class`——两臂使用相同的原生模型类。

## 运行

使用一键驱动脚本:

```bash
cd memory-bridge
head -2 instance-ids.txt > /tmp/first2-ids.txt
./utils/setup-run.sh /tmp/first2-ids.txt cure2
./utils/run-memory-arm.sh cure_memory          # 使用 output/LATEST
```

驱动脚本从 `.env` 读取 provider 信息（MODEL / API_KEY / BASE_URL / API）和
提取 LLM 设置（EXTRACT_MODEL / EXTRACT_BASE_URL / EXTRACT_API_KEY），为每个实例
启动一个
[traj-recorder](https://github.com/ChangranXU/traj-recorder/tree/memory)
roster 代理（MAIN lane = 基准模型，EXTRACT lane = 提取 LLM），运行桥接层，
然后依次执行合并、Docker 评测和汇总。

手动调用的详细说明见 [AGENTS.md](AGENTS.md)。

规则:每次调用一个锚定实例,`--workers 1`,每条臂使用全新 run root。
`EXTRACT_API_KEY` 只放在环境中,不要出现在命令行。

## 配置参考（`agent.memory.*`）

共享字段（`enabled`、`scope`、`user_id`、`output_dir`、`strict`、
`max_message_chars`、`inject_recall`、`max_memories`、`max_total_recall_chars`、
`max_chars_per_memory`、`annotate`、`annotate_*`）见[记忆生命周期](../../doc/memory-lifecycle.zh-CN.md)。
CURE 特有字段:

| 键 | 默认值 | 说明 |
|---|---|---|
| `db_path` | `""` | 显式 DB 路径（`""` = 按 scope 推导）。 |
| `cure_repo_path` | `""` | 显式 CURE 检出路径。`""` 依次回退到 `$CURE_MEMORY_REPO`、集成自身的 `src/` 树。显式路径与导入的包不匹配时会被拒绝。 |
| `extract_model` | `""` | 提取 LLM 模型名。`""` 回退到 `$EXTRACT_MODEL`。 |
| `extract_base_url` | `""` | 提取 LLM 端点。`""` 回退到 `$EXTRACT_BASE_URL`。 |
| `extract_api_key` | `""` | 提取 LLM API 密钥（从所有转储/repr 中排除）。`""` 回退到 `$EXTRACT_API_KEY`。三者必须全部设置,否则后端拒绝启动。 |
| `extract_every_n_steps` | `10` | 提取节奏。`0` = 仅最终冲刷。 |
| `extract_max_tokens` | `1600` | 映射到客户端 `max_completion_tokens`。 |
| `extract_reasoning_effort` | `low` | `""` 完全省略该参数（适用于拒绝该参数的端点）。 |
| `extract_timeout` | `60.0` | 每次提取尝试的 HTTP 超时。 |
| `extract_max_retries` | `1` | 客户端级重试（`0` = 单次尝试）。 |
| `extract_max_consecutive_errors` | `3` | 熔断器阈值。`0` = 永不熔断。 |

未知键会导致校验失败（`extra="forbid"`）。

## 产物

每个实例在 `--output` 目录下产出:

- **`memory.json`** — 回合日志:设置（已脱敏）、计数器
  （`messages_recorded`、`extraction_calls`/`errors`、决策计数、
  `recall_injections`、`backend_errors`）、事件、最终记忆状态。
- **`cure_memory.sqlite3`** — CURE 的 SQLite 存储（instance scope 在当前目录,
  run scope 在上一级）。
- **`<id>/<id>.traj.json`** — 原生轨迹外加 `info.memory` 统计。

## 轨迹标注

当 `annotate=true`（默认）且双 lane 均通过 roster 代理时,后端向
`trajectory.jsonl` 标注 schema-v6 `memory_*` 事件:会话/角色绑定、带变更审计的
生成操作、检索操作、投递证明。完整协议详见
[记忆追踪协议](../../doc/tracing.zh-CN.md)。

## 常见问题

| 症状 | 原因与解决 |
|---|---|
| memory.json 中 `available: false` | 查看 `op: "start"` 的 `error` 事件。可能是提取设置不完整（三个 `EXTRACT_*` 缺一不可）,或 `cure_memory` 不可导入（设置 `CURE_MEMORY_REPO`）。 |
| 事件中出现提取错误 | `llm_decision_failed:http_*` — 端点拒绝了调用。checkpoint 保持,下一次 tick 重试。连续失败达 `extract_max_consecutive_errors` 次后熔断。若 `reasoning_effort` 被拒绝,将其设为 `""`。 |
| 未提取到任何记忆 | 这是提取质量问题,不是框架故障。CURE 的敏感信息守卫会在 LLM 看到之前拒绝含 token/密码/密钥的消息。检查 `rejected_by_reason.sensitive_information`。 |
| 原始会话敏感性 | `record_message()` 在敏感守卫运行前就提交了内容。应像对待轨迹一样对待 `cure_memory.sqlite3`。 |
| run scope 污染 | 在同一共享 DB 中重跑中止的实例可能召回旧状态。每条臂务必使用全新 run root。 |

## 测试

完全离线（脚本化的假 decision 客户端），在 bundle 根目录运行:

```bash
cd memory-bridge && uv run python -m pytest integration/cure_memory/tests -q
```
