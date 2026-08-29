# shared-bridge

[memory-bridge](../README.zh-CN.md) 的通用核心,不依赖任何具体集成。提供智能体
钩子外壳、记忆后端生命周期骨架、标准化记忆端点契约,以及轨迹标注传输层。本包
源码中不出现任何具体记忆系统的名字——有一个
[专门的测试](tests/test_backend_base.py)
会扫描所有共享源码,一旦集成名称泄漏即构建失败,因此接入新的记忆系统无需改动
这里的代码。

[English](README.md) | [简体中文](README.zh-CN.md)

## 模块

| 模块 | 提供的能力 |
|---|---|
| [`agent.py`](src/shared_bridge/agent.py) | `MemoryAgent`——将记忆生命周期接入智能体循环。<br>录制每条轨迹消息,在干净步骤后触发提取,并在每次模型查询前注入召回的记忆(以瞬态 user 消息形式,模型可见但不会持久化)。<br>`agent.memory.enabled=false` 时退化为空壳,与基线逐字节一致。 |
| [`backend.py`](src/shared_bridge/backend.py) | `BaseMemoryBackend`——生命周期骨架:`start → set_task → record → maybe_extract → recall → finalize`。<br>生成 `memory.json` 产物,维护计数器,发射 schema-v6 记忆协议追踪。<br>所有合法的集成差异均通过显式钩子暴露。 |
| [`config.py`](src/shared_bridge/config.py) | `MemoryConfig`——共享的 `agent.memory.*` 字段(scope、提取节奏、召回预算、标注设置)。<br>集成通过子类化添加自己的字段。 |
| [`endpoint.py`](src/shared_bridge/endpoint.py) | 标准化 `MemoryEndpoint` 契约(`add` / `search` / `update` / `delete`)及其 pydantic 线上模型。<br>无需基准测试栈即可导入。 |
| [`serve.py`](src/shared_bridge/serve.py) | 纯标准库 HTTP 前端,将任意 `MemoryEndpoint` 暴露在 `/v1/memories/` 路由上。 |
| [`annotate.py`](src/shared_bridge/annotate.py) | 面向 [traj-recorder](https://github.com/ChangranXU/traj-recorder/tree/memory) 代理的标注传输层。<br>负责 lane URL 解析与校验、凭据安全的 URL 脱敏、分批发送、重试和熔断。 |
| [`run.py`](src/shared_bridge/run.py) | `bind_swebench_app()`——一行代码将原生 swebench 运行器的智能体类重绑到指定集成的智能体上。 |
| [`testing.py`](src/shared_bridge/testing.py) | `CaptureServer`——录制器 annotate 端点的本地 HTTP 替身。<br>供 bundle 内所有离线测试套件共用。 |

## 设计规则

- **仅标准库 + pydantic。**`minisweagent` 以惰性方式导入(用于智能体外壳与
  运行器工厂),确保端点契约在没有基准测试栈的环境中仍可直接导入。
- **失效封闭(fail-closed)。**后端错误被容纳并记录日志;除非设置了
  `config.strict`,否则异常不会抛入智能体循环。`note_recall` 绝不抛出异常
  ——可观测性决不能掩盖模型异常。
- **凭据绝不进入产物。**机密与 bearer-token 字段使用 `exclude=True,
  repr=False` 标记;日志中的 URL 仅保留脱敏形式(去掉
  userinfo/query/fragment,轨迹 ID 缩减为 16 位十六进制哈希前缀)。
- **标注是纯可观测性手段。**标注失败一律降级为无追踪的原生行为;标注
  I/O 耗时不计入智能体的 wall-time 预算。

## 在其上构建集成

1. **子类化 `BaseMemoryBackend`**,实现抽象钩子(存储 / 提取 / 检索 / 渲染,
   以及追踪适配三件套)——详见[记忆生命周期](../docs/zh/concepts/memory-lifecycle.md)。
2. **完成绑定:**子类化 `MemoryAgent` 并设置 `backend_class`(通常还需一个
   `MemoryConfig` 子类),再通过 `bind_swebench_app()` 接入运行器——详见
   [架构](../docs/zh/concepts/architecture.md)。若目标宿主不是 mini-swe-agent,
   用等价的运行器将智能体循环接入 `MemoryAgent` 子类即可——后端和端点层
   无需改动。
3. **将存储适配到共享契约**,实现一个 `MemoryEndpoint`——详见
   [记忆端点 API](../docs/zh/api-reference/overview.md)。

两个参考实现:[`integration/cure_memory`](../integration/cure_memory/README.md)
与 [`integration/mem0`](../integration/mem0/README.md)。

## 测试

所有测试均为离线测试,通过一个虚拟参考集成(`FakeBackend`)驱动,通用套件
不会触碰任何真实记忆系统:

```bash
cd <bundle-root> && uv run python -m pytest shared-bridge/tests -q
```

覆盖范围包括:标注传输、端点契约及其 HTTP 往返、后端生命周期(含失败路径的
产物钉死)、运行器工厂,以及零集成命名扫描。这些测试也可以在 bundle 根目录下
与两个集成套件一起通过单次调用运行。
