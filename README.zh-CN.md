# memory-bridge

**面向编程智能体的可插拔记忆层** — 以失效封闭（fail-closed）语义将记忆系统
挂载到智能体循环中：当记忆关闭或故障时，智能体的行为与原生版本完全一致。

[English](README.md) | [简体中文](README.zh-CN.md)

memory-bridge 管理完整的记忆生命周期——录制智能体消息、从中提取记忆、并将相关
上下文注入回智能体的提示词——作为模型不可见的宿主侧层。启用记忆时，召回的
记忆以瞬态 user 消息的形式在每次模型调用前注入（模型可见，但永不持久化）；
禁用记忆时，智能体与原生版本逐字节一致。每一项记忆决策都会作为一等标注事件
写入，供下游分析精确重建记忆的行为。

目前已集成
[mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent)，并在
[SWE-bench Verified](https://www.swebench.com/) 上进行评测。后端生命周期与
端点契约在设计上与具体智能体无关。

## 设计

- **失效封闭** — 记忆系统故障时自动降级为无追踪的原生行为；除非
  `strict: true`，任何异常都不会抛入智能体循环。
- **通用桥接层** — [`shared-bridge/`](shared-bridge/) 管理完整的记忆生命
  周期（record、extract、recall、finalize），代码中不出现任何具体集成的
  名称（由测试强制保证）。接入新的记忆系统只需在 `integration/` 下添加一个包。
- **标准化端点** — 统一的 `add` / `search` / `update` / `delete` HTTP 契约：
  同步写入，`user_id` 隔离。
  详见[端点 API](docs/zh/api-reference/overview.md)。
- **可移植内核** — 后端生命周期、端点契约和所有集成均与具体智能体无关。智能体
  钩子层（`MemoryAgent`）是约 200 行的薄适配器；适配到其他智能体时，后端和
  集成无需改动。

## 内置集成

| 集成 | 存储 | 提取方式 | 特性 |
|---|---|---|---|
| [CURE](integration/cure_memory/) | 本地 SQLite | 专用 LLM（EXTRACT lane） | 两层仓库/通用作用域 |
| [mem0 Platform](integration/mem0/) | 托管（[mem0.ai](https://mem0.ai)） | 平台侧 | 零基础设施 |
| [TencentDB-Agent-Memory](integration/tencentdb/) | 每 run 一个 MemoryCore 容器 | 服务端流水线 | 三个注入召回层（L1/L2/L3）+ 按需 L0 搜索 |

## 核心特性

- **查询改写器** — 可选的 side-model 将原始任务上下文改写为聚焦的检索查询，
  按可配置节奏触发，提升召回相关度。
- **脏标记检索缓存** — 仅在存储发生变更或查询被改写后才执行召回检索，消除冗余
  调用。
- **相关度下限** — `recall_min_score` 在任何数量限制之前过滤低分命中。
- **一等追踪** — 桥接层向
  [traj-recorder](https://github.com/ChangranXU/traj-recorder/tree/memory)
  代理写入 schema-v6 `memory_*` 标注事件，记录每一次提取、召回和投递。
- **内置 A/B 评测** — 在同一批 SWE-bench 实例上运行基线臂和记忆臂，由同一
  Docker 框架打分，得分差异可直接归因于记忆系统。

## 快速开始

### 前置条件

- Python 3.10+
- [`uv`](https://docs.astral.sh/uv/)
- Docker 已安装并正在运行（`docker info` 正常返回）
- 仓库根目录下有 provider `.env`（见下文）

### 伴生仓库

以下三个伴生仓库需要在运行流水线之前克隆，它们都不属于本仓库：

- [mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent) — 预测运行器；
  是共享环境的可编辑依赖（`swebench.yaml` 位于该检出中）。
- [SWE-bench](https://github.com/SWE-bench/SWE-bench) — 本地评测所需。
- [traj-recorder](https://github.com/ChangranXU/traj-recorder/tree/memory)
  （memory 分支）— 记忆臂所需。

```bash
# 请在 uv sync 之前克隆
git clone https://github.com/SWE-agent/mini-swe-agent mini-swe-agent
git clone https://github.com/SWE-bench/SWE-bench SWE-bench
git clone -b memory https://github.com/ChangranXU/traj-recorder.git extension/traj-recorder
```

SWE-bench 与 traj-recorder 也可放在仓库的同级目录；mini-swe-agent 则必须位于
仓库内的 `mini-swe-agent/`。

### 配置与运行

```dotenv
# .env（切勿提交）
MODEL=deepseek-v4-flash
API_KEY=sk-...
BASE_URL=https://api.deepseek.com
API=openai-chat
```

```bash
uv sync                                                              # 唯一的共享环境
uv run python -m pytest shared-bridge/tests integration/cure_memory/tests \
  integration/mem0/tests integration/tencentdb/tests -q              # 离线测试（不调用模型）

# 基线臂（原生 mini-swe-agent，无记忆）
./utils/setup-run.sh
./utils/run-predictions.sh && ./utils/merge-predictions.sh && \
  ./utils/run-evaluation.sh && ./utils/summarize-report.sh

# 记忆臂（任选一个集成）
head -2 instance-ids.txt > /tmp/first2-ids.txt
./utils/setup-run.sh /tmp/first2-ids.txt first2
./utils/run-memory-arm.sh cure_memory        # 或：mem0（MEM0_API_KEY 放在 integration/mem0/.env）
                                             # 或：tencentdb（需 Docker；可选 EMBEDDING_* 四件套放 .env）
```

## 解读结果

`summarize-report.sh` 输出 resolved / unresolved / error 三类判定。健康的运行
应满足 submitted == completed == 实例总数。

记忆臂还需检查每个实例的 `memory.json`：`enabled: true`、`available: true`、
`counts.extraction_errors: 0`，且在首条记忆通过审批后 `counts.recall_injections > 0`。当
`scope=run` 时，第二个实例从第一次模型调用起即可进行召回——通用记忆在整个
运行内可见，仓库绑定记忆仅在其所属仓库内可见（见
[CURE 集成](integration/cure_memory/README.zh-CN.md#适用性分层scoperun)）。关于运行产物与流水线
各阶段的详细说明，见[架构](docs/zh/concepts/architecture.md)。

## 仓库结构

```text
# 记忆层
shared-bridge/            通用桥接层：智能体钩子、后端生命周期、端点契约、追踪传输、
                            查询改写器、检索缓存、提示词归档
integration/cure_memory/  CURE 集成（本地 SQLite + 提取 LLM）
integration/mem0/         mem0 Platform 集成（托管）
integration/tencentdb/    TencentDB-Agent-Memory 集成（MemoryCore 容器）

# SWE-bench 评测框架
utils/                    流水线脚本：setup → predict → merge → evaluate → summarize
instance-ids.txt          有序实例列表（流水线默认输入）
mini-swe-agent/           预测运行器（伴生克隆，见"快速开始"）
output/                   运行根目录（由 setup-run.sh 创建）

# 文档
docs/                     GitBook 文档站（en/ + zh/ 语言变体）
```

## 文档

如需深入了解系统设计，建议从架构概览开始，再逐步阅读以下各篇：

- [架构](docs/zh/concepts/architecture.md) — 分层设计、SWE-bench 评测双臂、流水线
  阶段、运行产物、共享环境与查询改写器。
- [记忆生命周期](docs/zh/concepts/memory-lifecycle.md) — 智能体钩子、后端骨架及其
  钩子接口、检索缓存、查询改写、失效纪律。
- [记忆端点 API](docs/zh/api-reference/overview.md) — 标准化契约及其 HTTP 前端。
- [记忆追踪协议](docs/zh/concepts/tracing-protocol.md) — schema-v6 标注事件。
- [路线图](docs/zh/roadmap.md) — 未来开发计划。
- 各包指南：[shared-bridge](shared-bridge/README.zh-CN.md)、
  [cure_memory](integration/cure_memory/README.zh-CN.md)、
  [mem0](integration/mem0/README.zh-CN.md)、
  [tencentdb](integration/tencentdb/README.zh-CN.md)、
  [utils](utils/README.zh-CN.md)。

## 相关仓库

- [traj-recorder](https://github.com/ChangranXU/traj-recorder/tree/memory) —
  带角色分组 lane 与 annotate 端点的录制代理。
