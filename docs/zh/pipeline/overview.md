---
description: SWE-bench 评测流水线及其各阶段。
---

# 流水线

memory-bridge 包含完整的评测流水线，用于在 SWE-bench Verified 上运行 A/B 对比。所有脚本位于 `utils/`。记忆臂与合并/摘要阶段通过 bundle 的共享 uv 环境运行；基线预测运行在 mini-swe-agent 自己的环境中（附加临时的 `litellm[proxy]` overlay），评测则通过 SWE-bench 检出的环境运行。

## 两种评测流程

{% columns %}
{% column width="50%" %}
### 基线臂

原始 mini-swe-agent，无记忆——建立控制分数。

```text
setup-run.sh
  → run-predictions.sh
  → merge-predictions.sh
  → run-evaluation.sh
  → summarize-report.sh
```
{% endcolumn %}

{% column width="50%" %}
### 记忆臂

一个集成，记忆开启 `scope=run`——衡量记忆系统的影响。

```text
setup-run.sh
  → run-memory-arm.sh <integration>
    （串联 predict → merge → eval → summarize）
```
{% endcolumn %}
{% endcolumns %}

## 流水线脚本

| 脚本 | 描述 |
|---|---|
| `setup-run.sh` | 创建带时间戳的 run root `output/<name>-<ts>/`，记录在 `output/LATEST`。 |
| `run-predictions.sh` | 每次一个实例生成基线预测。 |
| `run-memory-arm.sh` | 一个集成的完整记忆臂（预测 + 合并 + 评测 + 摘要）。 |
| `merge-predictions.sh` | 验证并合并每实例 `preds.json` 为 `merged-preds.json`。 |
| `run-evaluation.sh` | 使用本地 SWE-bench Docker 评测框架打分。 |
| `summarize-report.sh` | 打印已解决 / 未解决 / 错误判定。 |
| `summarize-memory.sh` | 聚合 `memory.json` 文件为每 episode 表格。 |
| `validate_run.py` | 单个轨迹目录的离线验证（任意记忆臂）。 |
| `common.sh` | 共享辅助函数：run-root 解析、`.env` 加载、ids 文件读取。 |

{% hint style="info" %}
五个阶段脚本 `run-predictions.sh`、`merge-predictions.sh`、`run-evaluation.sh`、`summarize-report.sh` 和 `summarize-memory.sh` 接受 run root 作为可选的第一个参数。默认解析：`$RUN_ROOT` → `output/LATEST` 中记录的最新 root。`run-memory-arm.sh` 的第一个参数是集成，run root 是第二个参数：`run-memory-arm.sh <integration> [RUN_ROOT]`。
{% endhint %}

## Run root 结构

| 路径 | 写入者 | 内容 |
|---|---|---|
| `runs/mini-swe-agent/<id>/preds.json` | 预测 | 实例补丁 |
| `runs/mini-swe-agent/<id>/agent.log` | 记忆臂 | 完整智能体记录（驱动器重定向） |
| `runs/mini-swe-agent/<id>/minisweagent.log` | 预测 | 原始 mini-swe-agent 日志 |
| `runs/mini-swe-agent/<id>/memory.json` | 记忆臂 | Episode 日志：设置、计数器和事件 |
| `runs/mini-swe-agent/<id>/<id>/<id>.traj.json` | 预测 | 标准轨迹；`info.memory` 统计仅记忆臂 |
| `runs/mini-swe-agent/<id>/<id>/trajectory/` | 记忆臂 | 带 `memory_*` 事件的 traj-recorder 录制 |
| `runs/mini-swe-agent/cure_memory.sqlite3` | CURE 臂 | run 级别共享记忆存储 |
| `runs/mini-swe-agent/merged-preds.json` | 合并 | 所有已验证的补丁 |
| `local-eval/` | 评测 | SWE-bench 评测框架报告 |
| `memory-arm.log` | 记忆臂 | 驱动器日志 |

## 关键规则

{% hint style="danger" %}
违反这些规则将产生无效或被污染的结果。
{% endhint %}

1. **每个臂使用新 run root**——`scope=run` 时记忆存储在实例间共享；脏 root 污染臂。
2. **严格运行列出的实例，每次一个**——锚定 `^id$` 过滤，`--workers 1`。
3. **合并通过前不要评测**——缺失/空补丁使报告分母产生误导。
4. **仅本地评测**——始终使用 SWE-bench Docker 评测框架（`run-evaluation.sh`），不用 `sb-cli`。
5. **切勿复用死代理的 `.proxy_env_role*`**——用 SIGINT（不是 SIGKILL）关闭代理以便其完成 run 目录。
