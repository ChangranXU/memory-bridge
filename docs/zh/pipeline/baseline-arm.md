---
description: 运行基线臂（原始智能体，无记忆）。
---

# 基线臂

基线臂运行原始 mini-swe-agent，不使用记忆层——为 A/B 对比建立控制分数。

## 快速运行

```bash
# 设置 run root（默认使用 instance-ids.txt）
./utils/setup-run.sh

# 运行全部四个阶段
./utils/run-predictions.sh
./utils/merge-predictions.sh
./utils/run-evaluation.sh
./utils/summarize-report.sh
```

## 阶段详情

{% stepper %}
{% step %}
### 阶段 0——设置

```bash
./utils/setup-run.sh [IDS_FILE] [NAME]
```

创建带时间戳的 run root `output/<name>-<ts>/`，包含 `runs/`、`local-eval/` 和 ids 文件的副本。路径记录在 `output/LATEST`。
{% endstep %}

{% step %}
### 阶段 1——预测

```bash
./utils/run-predictions.sh [RUN_ROOT]
```

对 ids 文件中的每个实例运行 mini-swe-agent，每次一个，使用锚定的 `^id$` 过滤。完成时验证每个 `preds.json`。

roster `.env` 格式自动映射：`API_KEY`/`BASE_URL` → `OPENAI_API_KEY`/`OPENAI_BASE_URL`。
{% endstep %}

{% step %}
### 阶段 2——合并

```bash
./utils/merge-predictions.sh [RUN_ROOT]
```

验证并合并每实例 `preds.json` 文件为 `merged-preds.json`。缺失、为空或不一致的内容会导致失败。
{% endstep %}

{% step %}
### 阶段 3——评测

```bash
./utils/run-evaluation.sh [RUN_ROOT]
```

使用本地 SWE-bench Docker 评测框架打分。需要 Docker 运行和 SWE-bench 检出。

{% hint style="warning" %}
始终使用本地 Docker 评测框架（`run-evaluation.sh`），不用 `sb-cli`。
{% endhint %}
{% endstep %}

{% step %}
### 阶段 4——摘要

```bash
./utils/summarize-report.sh [RUN_ROOT]
```

打印已解决 / 未解决 / 错误判定：

* **已解决**——补丁已应用且测试通过
* **未解决**——补丁已应用但测试失败（模型未命中）
* **错误**——评测故障（在判断模型前先阅读每实例日志）
{% endstep %}
{% endstepper %}

## 环境

基线臂使用 mini-swe-agent 自己的 env 加上临时的 `litellm[proxy]` overlay。bundle 根目录的提供商 `.env` 提供：

```dotenv
MODEL=deepseek-v4-flash
API_KEY=sk-...
BASE_URL=https://api.deepseek.com
API=openai-chat
```
