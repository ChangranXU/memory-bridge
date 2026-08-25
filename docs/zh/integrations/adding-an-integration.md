---
description: 如何向 memory-bridge 添加新的记忆系统。
---

# 添加新集成

添加新记忆系统意味着在 `integration/<name>/` 下创建一个独立的包，绑定到通用桥接层。`shared-bridge/` 中不需要任何改动——零命名扫描测试强制执行此要求。

## 步骤

{% stepper %}
{% step %}
### 创建集成包

创建 `integration/<name>/` 作为 uv 工作空间成员，结构如下：

```text
integration/<name>/
├── pyproject.toml          # 工作空间成员
├── src/<name>_bridge/
│   ├── __init__.py
│   ├── backend.py          # BaseMemoryBackend 子类
│   ├── agent.py            # MemoryAgent 子类
│   ├── config.py           # MemoryConfig 子类（集成字段）
│   ├── endpoint.py         # MemoryEndpoint 适配器
│   ├── prompts.py          # 提示词的归属地；对于内嵌记忆系统，它也可能放在系统包中（CURE 即是如此）
│   └── run/
│       └── swebench.py     # 运行器模块
├── configs/                # 默认配置文件
└── tests/                  # 离线测试套件
```

在根 `pyproject.toml` 中注册该包为工作空间成员。
{% endstep %}

{% step %}
### 实现后端

继承 `BaseMemoryBackend` 并实现所有抽象钩子：

```python
from shared_bridge.backend import BaseMemoryBackend

class MyMemoryBackend(BaseMemoryBackend):
    def _resolve_settings(self) -> dict:
        """验证配置和环境；对预期的不可用性抛出异常。"""
        ...

    def _startup(self, settings: dict) -> None:
        """构造记忆系统。"""
        ...

    def _store_message(self, role: str, text: str, step: int) -> None:
        """持久化一条规范化消息。"""
        ...

    def _perform_extraction(self, step: int) -> None:
        """运行一次提取周期。"""
        ...

    def _search(self) -> list:
        """返回当前查询的召回命中。"""
        ...

    # ... 实现所有其余抽象钩子
```
{% endstep %}

{% step %}
### 绑定智能体

创建 `MemoryAgent` 子类，绑定 `backend_class` 和（通常）`config_class`：

```python
from shared_bridge.agent import MemoryAgent, MemoryAgentConfig
from .backend import MyMemoryBackend

class MyMemoryAgent(MemoryAgent):
    backend_class = MyMemoryBackend
    config_class = MemoryAgentConfig  # 或带有额外字段的子类
```

创建调用 `bind_swebench_app(MyMemoryAgent)` 的运行器模块 `run/swebench.py`。
{% endstep %}

{% step %}
### 实现端点适配器

```python
from shared_bridge.endpoint import (
    MemoryEndpoint, AddRequest, AddResponse,
    SearchRequest, SearchResponse,
    UpdateRequest, UpdateResponse, DeleteResponse,
)

class MyEndpoint(MemoryEndpoint):
    def add(self, request: AddRequest) -> AddResponse: ...
    def search(self, request: SearchRequest) -> SearchResponse: ...
    def update(self, memory_id, request, *, user_id=None) -> UpdateResponse: ...
    def delete(self, memory_id, *, user_id=None) -> DeleteResponse: ...
```

端点和后端的 `_search()` 必须共享同一语义——一次原生调用，相同的排序/过滤行为。
{% endstep %}

{% step %}
### 添加离线测试

按照与现有套件相同的风格编写测试：

* **无模型调用、无 Docker**——使用脚本化 fake 和 mock 传输
* **固定故障路径**，不仅仅是正常路径
* **命名测试 `test_<behavior>`**——每个测试应测试一个真实的故障点
* 测试必须从 bundle 根目录通过单次 pytest 调用运行
{% endstep %}

{% step %}
### 验证

从 bundle 根目录运行完整离线套件：

```bash
uv run python -m pytest shared-bridge/tests integration/<name>/tests -q
```

确认 `shared-bridge/tests` 中的零命名扫描仍然通过——它验证没有共享源代码命名你的集成（新增集成意味着把它的名字加进扫描的词边界模式——一处仅涉及测试的一行改动）。
{% endstep %}
{% endstepper %}

## 规则

{% hint style="danger" %}
这些约束不可商量：
{% endhint %}

1. `shared-bridge/` 中不能出现集成名
2. 集成必须是 uv 工作空间成员——永不拥有自己的 venv
3. 端点和后端 `_search()` 必须共享语义
4. 提示词文本存放在集成自己的 `prompts.py` 中，不能内联
5. 凭据使用 pydantic `exclude=True, repr=False`
