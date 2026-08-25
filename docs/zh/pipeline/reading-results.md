---
description: 解读评测报告和记忆产物。
---

# 解读结果

## 评测报告

```bash
./utils/summarize-report.sh [RUN_ROOT]
```

报告打印三种判定类别：

| 判定 | 含义 |
|---|---|
| **已解决** | 补丁已应用，测试通过——模型解决了实例 |
| **未解决** | 补丁已应用，测试失败——模型未命中 |
| **错误** | 评测故障——在判断模型前先阅读每实例日志 |

{% hint style="success" %}
健康的运行应有 `submitted == completed == 实例数`。如果出现错误，在得出关于模型质量的结论前，请先检查摘要指向的每实例日志。
{% endhint %}

## 记忆产物

### `memory.json`

每个记忆臂实例在轨迹旁生成一个 `memory.json`。需要检查的关键字段：

| 字段 | 健康值 | 问题指示 |
|---|---|---|
| `enabled` | `true` | `false` 表示记忆已关闭 |
| `available` | `true` | `false` 表示后端启动失败 |
| `counts.extraction_errors` | `0` | 非零表示某些提取失败 |
| `counts.recall_injections` | `> 0`（首次批准记忆后） | `0` 表示没有记忆到达模型 |
| `counts.recall_cache_hits` | 任意值 | 高值表示缓存运作良好 |
| `counts.search_errors` | `0` | 非零表示搜索调用失败 |

### 记忆摘要

```bash
./utils/summarize-memory.sh [RUN_ROOT]
```

聚合 run root 下的每个 `memory.json` 为每 episode 表格：

* **存储变化量**——每 episode 添加 / 更新 / 删除的记忆数
* **智能体主动读取**——tencentdb 臂的 L2 场景读取与 L0 对话搜索，每次都是被观测的真实智能体步骤
* **注入统计**——注入记忆的数量和字符预算
* **缓存命中率**——从检索缓存提供的召回步骤比例
* **改写结果**——查询改写器的成功/失败计数
* **跨 episode 召回比例**——来自每命中来源列表，有多少召回来自其他 episode vs. 当前 episode

### 轨迹验证（任意记忆臂）

```bash
uv run python utils/validate_run.py <run-dir>
```

验证一个录制的代理运行目录（保存 `trajectory.jsonl` 与 `run.json` 的 `<ts>-memory-<hash>/` 目录，位于 `<id>/<id>/trajectory/` 之下）：
* 事件顺序正确性
* 代理来源标签存在
* 记忆索引可提取性
* `run.json` 计数器一致性

## A/B 对比

要将分数差异归因于记忆系统：

1. 在**相同的有序实例列表**上运行两个臂
2. 使用**不同的 run root**（每个臂使用新 root）
3. 使用**相同的 Docker 评测框架**（`run-evaluation.sh`）打分
4. 并排比较 `summarize-report.sh` 输出

分数差异可归因于记忆系统本身，因为所有其他变量（模型、工具、模板、评测框架）保持不变。
