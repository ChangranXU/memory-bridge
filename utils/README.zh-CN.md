# utils/ — 流水线脚本

[English](README.md) | [简体中文](README.zh-CN.md)

所有流水线入口均在此目录下。每个脚本根据自身位置解析 bundle
根目录，加载 `common.sh` 中的公共函数，并通过 bundle 的共享 uv
环境运行。支持两条流水线：

- **基线臂**（原生 mini-swe-agent，不启用记忆）：
  `setup-run.sh` → `run-predictions.sh` → `merge-predictions.sh` →
  `run-evaluation.sh` → `summarize-report.sh`。
- **记忆臂**（选定一个集成，记忆开启，`scope=run`）：
  `setup-run.sh` → `run-memory-arm.sh`，后者自动串联预测、合并、评测与结果汇总。

## 脚本一览

| 脚本 | 说明 |
|---|---|
| `setup-run.sh` | 创建带时间戳的运行目录 `output/<name>-<ts>/`（含 `runs/`、`local-eval/` 子目录及 ids 文件副本），并将路径记录到 `output/LATEST`。用法：`setup-run.sh [IDS_FILE] [NAME]`。 |
| `run-predictions.sh` | 逐个实例生成基线预测（锚定匹配），完成后校验所有 `preds.json`。 |
| `run-memory-arm.sh` | 完整执行单个集成的记忆臂流程：`run-memory-arm.sh <cure_memory\|mem0\|tencentdb> [RUN_ROOT]`。每个实例在专属的 [traj-recorder](https://github.com/ChangranXU/traj-recorder/tree/memory) roster 代理后运行（MAIN 通道 = 基准模型；role 2 = cure_memory 的 EXTRACT 通道，或 mem0/tencentdb 的 MEMORY 通道——零模型调用的标注命名空间）。mem0 臂的部署模式归 yaml 所有（`integration/mem0/configs/memory_defaults.yaml` 中的 `mode:` 行）：`platform`（托管；根目录 `.env` 中的 `MEM0_API_KEY`）、`server`（每 run 的双容器 OSS 栈，位于 `127.0.0.1:8890`；需要 Docker 及 `EMBEDDING_*` 四件套）或 `library`（经可选依赖组 `mem0-library` 的进程内引擎；需要四件套）。tencentdb 臂还会为每个运行目录管理一个 MemoryCore 容器（自动生成的 gateway yaml、等待健康检查、退出时 `docker rm -f` 拆除；roster `.env` 中可选的 `EMBEDDING_*` 四件套须全设或全不设，用于启用向量通道）。恢复运行时自动跳过已有有效补丁（`preds.json` 的 `model_patch` 非空）的实例；若实例存在无有效补丁的残留记录则拒绝恢复。 |
| `merge-predictions.sh` | 校验各实例的 `preds.json` 并合并为 `merged-preds.json`；缺失、为空或不一致时报错终止。 |
| `run-evaluation.sh` | 使用本地 [SWE-bench](https://github.com/SWE-bench/SWE-bench) Docker 评测框架对合并后的预测评分（不使用 `sb-cli`）。 |
| `summarize-report.sh` | 输出最新评测报告的计数与实例列表，给出 resolved / unresolved / error 判定。 |
| `summarize-memory.sh` | 将运行目录下所有 `memory.json` 聚合为按集的表格：存储增量（added/updated/deleted）、注入量（次数/字符数）、搜索缓存命中率、改写结果、由逐条来源列表算出的跨集召回占比，以及注解通道降级计数（非零 = trajectory 记录的内存动作少于 memory.json 所示；在信任 trajectory 衍生数字前先核对两者）。只读（无模型调用、无 Docker）。 |
| `validate_run.py` | 离线校验单个录制的代理运行目录——保存 `trajectory.jsonl` 与 `run.json` 的 `<ts>-memory-<hash>/` 目录，位于 `<id>/<id>/trajectory/` 之下（任意记忆臂）：事件顺序、代理来源标签、记忆索引可提取性、`run.json` 计数器。用法：`validate_run.py <run-dir>`。 |
| `common.sh` | 各包装脚本共用的辅助函数：运行目录解析、roster `.env` 加载（`API_KEY`/`BASE_URL` → `OPENAI_*` 映射及模型名前缀处理）、ids 文件读取。 |
| `merge_predictions.py` / `summarize_report.py` / `summarize_memory.py` | 对应 `.sh` 包装脚本的核心逻辑——请通过包装脚本调用，不要直接运行。 |

各阶段脚本接受可选的首参数作为运行目录（默认依次取 `$RUN_ROOT`、`output/LATEST`
中记录的最新目录）。例外：`setup-run.sh`（创建运行目录）和
`validate_run.py`（接受录制运行目录）。

## 用法示例

```bash
# 基线臂（默认使用 instance-ids.txt）
./utils/setup-run.sh
./utils/run-predictions.sh && ./utils/merge-predictions.sh && \
  ./utils/run-evaluation.sh && ./utils/summarize-report.sh

# 记忆臂（任选一个集成）
head -2 instance-ids.txt > /tmp/first2-ids.txt
./utils/setup-run.sh /tmp/first2-ids.txt first2
./utils/run-memory-arm.sh cure_memory                 # 或：mem0，或：tencentdb（需要 Docker）
```

前置条件见根目录 [README](../README.zh-CN.md)；各阶段产物详见[架构文档](../docs/zh/concepts/architecture.md)。
