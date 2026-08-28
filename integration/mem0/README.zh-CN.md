# mem0 集成

**将 [mem0](https://github.com/mem0ai/mem0) 作为面向编程智能体的记忆系统——托管
Platform、自托管 OSS server 或进程内 library 三种部署模式。目前已接入
[mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent) 用于 SWE-bench
评测。**

[English](README.md) | [简体中文](README.zh-CN.md)

在结构上与 `integration/cure_memory` 一致,但将 CURE 的本地 SQLite 存储和提取
LLM 替换为 mem0,并支持三种部署模式之一(`configs/memory_defaults.yaml` 中的
`mode:` 行):

- `platform`(默认)—— 托管 API;提取在平台侧运行。
- `server` —— 每 run 一套自托管 OSS server 栈(pgvector + API server 两个
  容器);提取在容器内直连提供商上游运行。
- `library` —— 进程内 `mem0ai` 引擎;提取在本进程内直连提供商上游运行。

每种模式下提取流量都不进入轨迹,因此 memory lane 不承载模型流量——它仅作为
轨迹代理的标注命名空间,后端将 schema-v6 记忆协议从引擎回执投递到此处。

## 架构

```text
python -m mem0_bridge.run.swebench        # 运行器：重绑 ProgressTrackingAgent
  └─ Mem0Agent(MemoryAgent)               # shared-bridge 智能体外壳
      └─ Mem0Backend                      # 单回合生命周期
          ├─ record()        缓冲轨迹消息（截断、角色映射）
          ├─ maybe_extract() store.add() — platform：异步 + 事件轮询；
          │                  server/library：同步;失败时保留批次以便重试
          ├─ recall_context() store.search();rank-then-fill 渲染;
          │                  作为瞬态 user 消息注入
          └─ finalize()      最终冲刷 + get_all 转储 + memory.json + 关闭
              └─ Mem0Store（open_store 按模式分发,按模式惰性导入）
                  ├─ platform  Mem0PlatformClient (httpx) → 托管 v3/v1 API
                  ├─ server    ServerStore (httpx) → 127.0.0.1:8890 的每 run OSS 容器
                  └─ library   LibraryStore → 进程内 mem0ai 引擎
```

`Mem0Endpoint`(`mem0_bridge.endpoint`)将同一个 `Mem0Store` 适配到共享的
`MemoryEndpoint` 契约(add/search/update/delete):后端与端点消费同一套 store
协议,因此检索恰好按每模式一次原生调用实现两次,两个表面不会漂移。

## Stores

platform 与 server 模式通过 httpx 走 REST;library 模式在进程内运行引擎:

- **platform** —— `Mem0PlatformClient`:`Token` 鉴权、v3 add/search/get-all、
  v1 CRUD + 事件轮询;异步 add 轮询至完成(`poll_budget`/`poll_interval`)。
- **server** —— `ServerStore`(httpx):路由不带前缀、严格区分尾部斜杠;add
  为同步(请求内部包含一次提取 LLM 往返——`add_timeout` 默认 300 s);驱动脚本
  会将 server 臂的 `search_timeout` 提高到 30(一次 HTTP 调用掩盖了 embedder
  往返和混合检索的 CPU 开销)。
- **library** —— `LibraryStore`:`from mem0 import Memory`,仅在 library 模式
  下导入。`mem0ai` SDK 只能通过根目录可选依赖组 `mem0-library` 进入共享环境
  (`uv run --group mem0-library`;裸 `uv sync` 会将其移除)——默认环境保持无
  mem0ai,因为该 SDK 会把完整的开源栈(嵌入器、向量存储、DB 驱动)作为传递依赖
  引入,可能与 `litellm[proxy]` 冲突。

所有请求/响应结构均已对照线上平台 API 与 vendored OSS 代码树验证(pin 见
`VENDORING.md`)。

## 运行隔离

每种模式的运行隔离都依赖有效 user id;server/library 模式在此之上叠加全新的
每 run 存储(platform 的存储是托管的,在 run root 间持久):

- `scope=run`(默认):整个 run root 使用同一个 `user_id` — 实例 2 可以
  召回实例 1 的记忆。
- `scope=instance`:`"{user_id}:{instance_id}"` — 每个任务独立命名空间。

驱动脚本自动生成 `user_id=minisweagent-mem0-<run-root-basename>`(含时间戳),
因此全新 run root 不会召回上一次运行的记忆。仅传 `user_id`(而非
`agent_id`)作为实体 id — 否则引擎的归属拆分会导致 user 过滤的检索遗漏
assistant 消息中的事实。

## 运行

```bash
cd memory-bridge
head -2 instance-ids.txt > /tmp/first2-ids.txt
./utils/setup-run.sh /tmp/first2-ids.txt mem0first2
./utils/run-memory-arm.sh mem0                       # 使用 output/LATEST
```

模式来自 `configs/memory_defaults.yaml` 中带锚定的 `mode:` 行(归 yaml 所有——
驱动脚本读取同一行并拒绝 `--config agent.memory.mode=` 附加项;见
[AGENTS.md](AGENTS.md))。各模式前置条件:

- `platform`:bundle 根目录 `.env` 中的 `MEM0_API_KEY`。
- `server`:Docker 已安装并运行,且 bundle 根目录 `.env` 中配置完整的
  `EMBEDDING_MODEL` / `EMBEDDING_API_KEY` / `EMBEDDING_BASE_URL` /
  `EMBEDDING_DIMENSIONS` 四元组(失效封闭——OSS 引擎在每次 add/search 都要
  做嵌入,没有纯词法回退)。驱动脚本构建并管理双容器栈(发布在
  `127.0.0.1:8890`,机器级单臂占用),退出时移除;存储位于
  `<run-root>/mem0-server/`。
- `library`:同样的 `EMBEDDING_*` 四元组(失效封闭)。每次实例调用自身携带
  `uv run --group mem0-library`;可选的 `uv sync --group mem0-library` 预热只是
  省去逐实例的解析。存储位于 `<run-root>/mem0/`。

驱动脚本从 bundle 根目录 `.env` 读取 provider roster,然后为每个实例启动一个
[traj-recorder](https://github.com/ChangranXU/traj-recorder/tree/memory)
roster 代理(MAIN lane = 基准模型,MEMORY lane = 标注命名空间)。之后依次执行
合并、Docker 评测和汇总;存在残留尝试(agent.log 无有效 preds.json)的实例
会被拒绝恢复。

每实例产出:`preds.json`、`agent.log`、`memory.json`、`proxy.log`、
`<id>/<id>.traj.json`、`<id>/<id>/trajectory/`。

## 文件结构

```text
integration/mem0/
├── pyproject.toml                # uv 工作区成员
├── VENDORING.md                  # vendored 克隆：获取、pin、边界
├── configs/memory_defaults.yaml  # 部分 agent.memory.* 叠加（携带 mode: 行）
├── src/mem0_bridge/
│   ├── config.py                 # Mem0Config(MemoryConfig)
│   ├── client.py                 # Mem0PlatformClient (httpx;platform 模式)
│   ├── stores/
│   │   ├── __init__.py           # Mem0Store 协议 + open_store 工厂
│   │   ├── platform.py           # 托管 API store（包装 Mem0PlatformClient）
│   │   ├── server.py             # 每 run OSS server store (httpx)
│   │   └── library.py            # 进程内 mem0ai store（可选依赖组）
│   ├── backend.py                # Mem0Backend
│   ├── agent.py                  # Mem0Agent / Mem0AgentConfig
│   ├── endpoint.py               # Mem0Endpoint(MemoryEndpoint)
│   └── run/swebench.py           # 运行器：一行式智能体重绑
├── vendor/mem0/                  # gitignored 的 vendored OSS 克隆——绝不提交、绝不导入
└── tests/                        # 离线测试套件
```

由 bundle 根目录的 `uv sync` 作为可编辑工作区成员安装。

## 配置参考(`agent.memory.*`)

共享字段见[记忆生命周期](../../doc/memory-lifecycle.zh-CN.md)。
mem0 特有字段:

| 键 | 默认值 | 说明 |
|---|---|---|
| `mode` | `"platform"` | 部署模式选择:`platform` \| `server` \| `library`。归 yaml 所有——在 `configs/memory_defaults.yaml` 中设置,切勿通过 `--config` 附加项传递。 |
| `api_key` | `""` | platform 模式:mem0 API 密钥。`""` 回退到 `$MEM0_API_KEY`。 |
| `base_url` | `""` | platform 模式:`""` 依次回退到 `$MEM0_BASE_URL`、`https://api.mem0.ai`。 |
| `server_url` | `""` | server 模式:`""` 回退到 `$MEM0_SERVER_URL`(由驱动脚本按 run 生成)。 |
| `server_api_key` | `""` | server 模式:可选——臂以 `AUTH_DISABLED=true` 运行容器,空密钥则不发送鉴权头。 |
| `run_root` | `""` | library 模式:存储目录锚点(`<run_root>/mem0/`);驱动脚本传入 `$RUN_ROOT`。 |
| `infer` | `true` | 引擎侧提取。`false` 逐字存储消息。 |
| `search_threshold` | `0.0` | 在每个表面上显式发送;语义因表面而异——platform 的 `0.0` 禁用截止,OSS(server/library)的 `0.0` 是混合合并前对原始分数的最小门限。排序 + `max_memories` 约束召回。 |
| `poll_budget` | `60.0`(叠加中为 120) | 仅 platform 模式:每批次的 add+poll 总时间预算(OSS 的 add 为同步)。 |
| `poll_interval` | `1.0` | 仅 platform 模式:轮询异步 add 事件的间隔。 |

## 解读 memory.json

健康的运行应显示 `enabled: true`、`available: true`、`extraction_errors: 0`,
且在首次成功提取后 `recall_injections > 0`。`scope=run` 时,第二个实例从
step 0 起即可进行召回。settings 记录 `mode` 和 `bridge_version`;start 事件携带
`mode`。`counts` 还包含 `search_calls`/`search_errors` 和
`memories_added`/`updated`/`deleted`。`final_memories` 是有效用户记忆的诊断
转储。

## 测试

```bash
cd memory-bridge && uv run python -m pytest integration/mem0/tests -q
```

完全离线:后端测试使用脚本化的 store,客户端测试运行在
`httpx.MockTransport` 上,library 模式测试借助 fake `Memory` 接缝(套件从不
导入 `mem0ai`),智能体测试使用确定性 toolcall 模型。可与 shared-bridge 和
CURE 套件一起运行。
