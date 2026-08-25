# mem0 集成

**将 [mem0 Platform](https://github.com/mem0ai/mem0) 作为面向编程智能体的托管记忆系统。
目前已接入 [mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent) 用于
SWE-bench 评测。**

[English](README.md) | [简体中文](README.zh-CN.md)

在结构上与 `integration/cure_memory` 一致,但将 CURE 的本地 SQLite 存储和提取
LLM 替换为 mem0 的托管 API。memory lane 不承载模型流量——它仅作为轨迹代理的
标注命名空间,后端将 schema-v6 记忆协议从平台回执投递到此处。

## 架构

```text
python -m mem0_bridge.run.swebench        # 运行器：重绑 ProgressTrackingAgent
  └─ Mem0Agent(MemoryAgent)               # shared-bridge 智能体外壳
      └─ Mem0Backend                      # 单回合生命周期
          ├─ record()        缓冲轨迹消息（截断、角色映射）
          ├─ maybe_extract() POST /v3/memories/add/（平台侧提取），
          │                  轮询至完成；失败时保留批次以便重试
          ├─ recall_context() POST /v3/memories/search/；rank-then-fill 渲染；
          │                  作为瞬态 user 消息注入
          └─ finalize()      最终冲刷 + get_all 转储 + memory.json + 关闭
```

`Mem0Endpoint`（`mem0_bridge.endpoint`）将同一客户端适配到共享的
`MemoryEndpoint` 契约（add/search/update/delete）。

## REST 客户端设计

mem0 SDK 的 `MemoryClient` 会将完整的开源栈（嵌入器、向量存储、DB 驱动）作为
传递依赖引入,有可能与共享环境中的 `litellm[proxy]` 产生冲突。桥接层所需的
REST 接口很小（`Token` 鉴权、v3 add/search、v1 CRUD + 事件轮询），而 httpx
已在环境中可用。所有请求/响应结构均已对照线上 API 验证。

## 运行隔离

mem0 存储在 run root 间是持久的,因此隔离依赖于有效 user id:

- `scope=run`（默认）：整个 run root 使用同一个 `user_id` — 实例 2 可以
  召回实例 1 的记忆。
- `scope=instance`：`"{user_id}:{instance_id}"` — 每个任务独立命名空间。

驱动脚本自动生成 `user_id=minisweagent-mem0-<run-root-basename>`（含时间戳），
因此全新 run root 不会召回上一次运行的记忆。仅传 `user_id`（而非
`agent_id`）作为实体 id — 否则平台的归属拆分会导致 user 过滤的检索遗漏
assistant 消息中的事实。

## 运行

```bash
cd memory-bridge
head -2 instance-ids.txt > /tmp/first2-ids.txt
./utils/setup-run.sh /tmp/first2-ids.txt mem0first2
./utils/run-memory-arm.sh mem0                       # 使用 output/LATEST
```

驱动脚本从 `.env` 读取 provider 信息,从 `integration/mem0/.env` 读取
`MEM0_API_KEY`,然后为每个实例启动一个
[traj-recorder](https://github.com/ChangranXU/traj-recorder/tree/memory)
roster 代理（MAIN lane = 基准模型,MEMORY lane = 标注命名空间）。之后依次执行
合并、Docker 评测和汇总;存在残留尝试（agent.log 无有效 preds.json）的实例
会被拒绝恢复。

每实例产出：`preds.json`、`agent.log`、`memory.json`、`proxy.log`、
`<id>/<id>.traj.json`、`<id>/<id>/trajectory/`。

## 文件结构

```text
integration/mem0/
├── .env                          # MEM0_API_KEY（切勿提交）
├── pyproject.toml                # uv 工作区成员
├── configs/memory_defaults.yaml  # 部分 agent.memory.* 叠加
├── src/mem0_bridge/
│   ├── config.py                 # Mem0Config(MemoryConfig)
│   ├── client.py                 # Mem0PlatformClient (httpx)
│   ├── backend.py                # Mem0Backend
│   ├── agent.py                  # Mem0Agent / Mem0AgentConfig
│   ├── endpoint.py               # Mem0Endpoint(MemoryEndpoint)
│   └── run/swebench.py           # 运行器：一行式智能体重绑
└── tests/                        # 离线测试套件
```

由 bundle 根目录的 `uv sync` 作为可编辑工作区成员安装。

## 配置参考（`agent.memory.*`）

共享字段见[记忆生命周期](../../doc/memory-lifecycle.zh-CN.md)。
mem0 特有字段:

| 键 | 默认值 | 说明 |
|---|---|---|
| `api_key` | `""` | mem0 API 密钥。`""` 回退到 `$MEM0_API_KEY`。 |
| `base_url` | `""` | `""` 依次回退到 `$MEM0_BASE_URL`、`https://api.mem0.ai`。 |
| `infer` | `true` | 平台侧提取。`false` 逐字存储消息。 |
| `search_threshold` | `0.0` | 平台相关度截止阈值（`0.0` 禁用;排序 + `max_memories` 约束召回）。 |
| `poll_budget` | `60.0`（叠加中为 120） | 每批次的 add+poll 总时间预算。 |
| `poll_interval` | `1.0` | 轮询异步 add 事件的间隔。 |

## 解读 memory.json

健康的运行应显示 `enabled: true`、`available: true`、`extraction_errors: 0`,
且在首次成功提取后 `recall_injections > 0`。`scope=run` 时,第二个实例从
step 0 起即可进行召回。`counts` 还包含 `search_calls`/`search_errors` 和
`memories_added`/`updated`/`deleted`。`final_memories` 是有效用户记忆的诊断
转储。

## 测试

```bash
cd memory-bridge && uv run python -m pytest integration/mem0/tests -q
```

完全离线:后端测试使用脚本化的平台客户端,客户端测试运行在
`httpx.MockTransport` 上,智能体测试使用确定性 toolcall 模型。可与
shared-bridge 和 CURE 套件一起运行。
