---
description: 10 分钟内启动并运行 memory-bridge。
---

# 快速开始

本指南将引导你完成 memory-bridge 的安装、运行一次记忆增强评测，并检查结果。

## 前置条件

{% hint style="info" %}
在继续之前，请确保所有前置条件已安装。两个臂的预测和评测都需要 Docker；tencentdb 记忆臂还需在 Docker 中运行其 MemoryCore 容器。
{% endhint %}

* Python 3.10+
* [`uv`](https://docs.astral.sh/uv/) 在 PATH 中
* Docker 已安装并运行（`docker info` 成功）
* 提供商 API 密钥（如 DeepSeek、OpenAI）

## 安装

{% stepper %}
{% step %}
### 克隆伴生仓库

克隆三个伴生检出。其中只有 `mini-swe-agent` 必须在 `uv sync` 之前就位（它是可编辑路径依赖）；SWE-bench 在运行任何流水线脚本之前需要，traj-recorder 仅在运行记忆臂之前需要：

```bash
# 在 memory-bridge 目录内
git clone https://github.com/SWE-agent/mini-swe-agent mini-swe-agent
git clone https://github.com/SWE-bench/SWE-bench SWE-bench
git clone -b memory https://github.com/ChangranXU/traj-recorder.git extension/traj-recorder
```

`mini-swe-agent` 必须位于仓库根目录的 `mini-swe-agent/` 内。SWE-bench 也可以位于 `<parent>/SWE-bench`（仓库的同级目录），traj-recorder 也可以位于 `<parent>/extension/traj-recorder`（父目录的 `extension/` 子目录内）。
{% endstep %}

{% step %}
### 创建提供商 `.env`

在仓库根目录创建 `.env` 文件（切勿提交此文件）：

```dotenv
MODEL=deepseek-v4-flash
API_KEY=sk-...
BASE_URL=https://api.deepseek.com
API=openai-chat
```

CURE 臂会通过每实例代理自动接入其提取 lane，复用 `MODEL`——无需额外的 `.env` 键。（当集成在记忆臂驱动器之外运行时，`EXTRACT_*` 环境变量仍是后端回退；参见 [配置参考](configuration.md)。）

对于 mem0 臂，创建 `integration/mem0/.env`：

```dotenv
MEM0_API_KEY=m0-...
```
{% endstep %}

{% step %}
### 安装共享环境

```bash
uv sync
```

这会在 bundle 根目录创建唯一的 `.venv`，包含所有工作空间成员（shared-bridge、集成）和依赖项（mini-swe-agent、litellm\[proxy]）。

{% hint style="warning" %}
始终在 bundle 根目录运行 `uv sync`——切勿在集成目录内运行。
{% endhint %}
{% endstep %}

{% step %}
### 验证安装

运行离线测试套件以确认一切正常：

```bash
uv run python -m pytest shared-bridge/tests integration/cure_memory/tests integration/mem0/tests integration/tencentdb/tests -q
```

这些测试不使用模型调用也不使用 Docker——它们使用脚本化的 fake 和本地捕获服务器运行。
{% endstep %}
{% endstepper %}

## 运行首次评测

### 设置 run root

```bash
# 使用小切片快速测试
head -2 instance-ids.txt > /tmp/first2-ids.txt
./utils/setup-run.sh /tmp/first2-ids.txt first2
```

这会在 `output/` 下创建一个带时间戳的目录，并将其记录在 `output/LATEST` 中。

### 运行记忆臂

{% tabs %}
{% tab title="CURE" %}
```bash
./utils/run-memory-arm.sh cure_memory
```

CURE 臂使用本地 SQLite 存储和专用提取 LLM。每个实例会启动一个 roster 代理，包含 MAIN（基准模型）、EXTRACT（提取 LLM）和 QUERY（召回查询改写器）lane。
{% endtab %}

{% tab title="mem0" %}
```bash
./utils/run-memory-arm.sh mem0
```

mem0 臂使用托管的 mem0 Platform 进行提取。需要在 `integration/mem0/.env` 中设置 `MEM0_API_KEY`。
{% endtab %}

{% tab title="tencentdb" %}
```bash
./utils/run-memory-arm.sh tencentdb
```

需要 Docker 运行。驱动器会为每个 run root 启动一个 MemoryCore 容器，退出时自动移除；可选的 embedding lane 由 roster `.env` 中全有或全无的 `EMBEDDING_*` 四元组启用。
{% endtab %}
{% endtabs %}

### 运行基线臂（可选，用于 A/B 对比）

```bash
./utils/setup-run.sh /tmp/first2-ids.txt baseline
./utils/run-predictions.sh
./utils/merge-predictions.sh
./utils/run-evaluation.sh
./utils/summarize-report.sh
```

### 检查结果

```bash
# 打印已解决 / 未解决 / 错误判定
./utils/summarize-report.sh

# 聚合跨 episode 的记忆行为
./utils/summarize-memory.sh
```

健康的记忆臂实例在 `memory.json` 中应显示 `enabled: true`、`available: true`、`counts.extraction_errors: 0`，以及 `counts.recall_injections > 0`。

## 下一步

* 阅读 [架构](../concepts/architecture.md) 了解分层设计
* 查看 [API 参考](../api-reference/overview.md) 了解标准化端点契约
* 参见 [配置参考](configuration.md) 了解所有可用设置
