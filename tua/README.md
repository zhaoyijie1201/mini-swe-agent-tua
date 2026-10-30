# TUA-Bench 成本感知 harness（PE6203 Project 5）运行手册

这是 mini-swe-agent v2.4.6 的 fork（分支 `tua-harness`，版本号 `2.4.6+tua.1`）。
在原版之上只增加了文件，不改动原有行为：**所有开关关闭时与 PyPI 2.4.6 完全一致**（有离线测试保证）。

| 位置 | 内容 |
|---|---|
| `src/minisweagent/agents/tua.py` | `TuaAgent`：C1–C4 四个代码开关 |
| `src/minisweagent/agents/utils/tua_utils.py` | 去噪、截断、压缩、停滞检测、提交前检查脚本等纯函数 |
| `src/minisweagent/config/tua/base.yaml` | 等于 `mini.yaml`（默认 harness 的配置） |
| `src/minisweagent/config/tua/prompt_tua.yaml` | C5：面向终端任务的通用提示词 |
| `src/minisweagent/config/__init__.py` | 唯一改动的原有文件：配置查找多加一个候选目录 `config/tua/`（一行） |
| `tua/configs/*.yaml` | 每个实验配置一份完整 yaml（由 `gen_configs.py` 生成） |
| `tua/configs/subset-40.json` | 固定的 40 题子集（seed 6203，仅 `cpus <= 2` 的任务，按类别分层） |
| `tua/harbor_agent.py` | Harbor 自定义 agent：把本 fork 的 wheel 装进任务容器 |
| `tua/scripts/patch_harbor.py` | Harbor 0.6.3 的两个运行器补丁 |
| `tua/scripts/run_config.py` | 生成/执行某个配置的 `harbor run` 命令 |
| `tua/scripts/summarize.py` | 从原始日志算成功率、token、美元，画 Pareto 图 |
| `tua/pricing/prices.json` | 固定价表（OpenRouter 2026-10-30 标价） |
| `tests/agents/test_tua.py`, `tests/config/test_tua_configs.py` | 离线测试（无容器、无模型调用） |
| `tua/HANDOVER.md`, `tua/PROPOSAL.md` | 交接文档（改进点、进度、注意事项）和已批准的改进方案 |

## 开关与配置矩阵

| 开关（`agent.*`） | 组件 | 作用 |
|---|---|---|
| `budget_notices` | C1 预算可见 | 每条观测末尾附 `[budget] step k/150 \| elapsed \| time left`，用掉 80% 预算时注入一次收尾提醒；配套 `step_limit: 150`、`wall_time_limit_seconds: 2200` |
| `stall_detection` | C2 停滞检测与恢复 | 同一命令 ≥3 次 / 连续相同输出 / 命令超时时注入提示；配套 `max_consecutive_format_errors: 10`、`environment.timeout: 120`、更友好的无工具调用恢复提示 |
| `verify_gate` | C3 提交前自检门 | 第一次提交被拦截，容器内只读检查（任务里提到的路径是否存在、任务开始后改动了哪些文件、Office 文件能否打开、xlsx 公式是否缺缓存值），把报告交回模型，第二次提交放行 |
| `observation_hygiene` | C4 观测去噪与压缩 | 去掉 dbus/GTK/fontconfig/chromium 噪声行、折叠重复行、按行截断；发给模型时只保留最近 8 条观测全文，更早的只留 400 字符（轨迹文件保存全文） |
| （提示词） | C5 | `prompt_tua.yaml` 替换 system/instance 模板，不含任何任务信息 |

| 配置 | harness | 模型 | 用途 |
|---|---|---|---|
| `B0-qwen` | **原版 PyPI 2.4.6**（Harbor 自带 `mini-swe-agent` agent） | Qwen3.7 Flash | 便宜模型基线 |
| `B1-dsflash` | 原版 PyPI 2.4.6 | DeepSeek V4.1 Flash (1017) | 昂贵模型基线 |
| `A0-qwen` | fork，全关 | Qwen | 可选：验证 fork 全关 ≈ B0 |
| `A1`–`A5-qwen` | fork，只开 C1…C5 之一 | Qwen | 单组件消融 |
| `H-qwen` | fork，全开 | Qwen | 组合版 |
| `H-dsflash` | fork，全开 | DeepSeek Flash | 可选：增益是否随模型变强而缩小 |

模型都通过 OpenRouter，供应商被固定（Qwen→Alibaba，DeepSeek→DeepSeek，`allow_fallbacks: false`），Qwen 关闭 reasoning。同一模型的所有配置使用完全相同的模型参数。

## 环境准备（Linux 或 WSL2，需要 Docker）

```bash
# 1. 代码
git clone <本 fork 的地址> mini-swe-agent && cd mini-swe-agent && git checkout tua-harness
git clone https://github.com/facebookresearch/TUA-Bench ../TUA-Bench
git -C ../TUA-Bench checkout 3497fd320abcafaf4797424192c891a593fd7964   # subset-40.json 对应的提交

# 2. TUA-Bench 自带的环境（含 harbor==0.6.3），并下载任务资产
cd ../TUA-Bench && uv sync && uv run setup-env && cd ../mini-swe-agent

# 3. 给 TUA-Bench 环境里的 Harbor 打补丁（幂等，可重复执行）
(cd ../TUA-Bench && uv run python ../mini-swe-agent/tua/scripts/patch_harbor.py)

# 4. 构建本 fork 的 wheel（会被上传进每个任务容器）
python -m pip wheel --no-deps -w tua/dist .

# 5. 密钥
export OPENROUTER_API_KEY=sk-or-...
```

补丁内容（都不改任务和验证器，对所有配置一致）：① Harbor 对日志目录的 `chmod 777` 改为以 root 执行并包含 artifacts 目录，否则 TUA 镜像（默认非 root 用户）下验证器写不出 reward；② 任务容器启动后删除 `/usr/local/bin/chrome_state.py`，它的 `solved` 模式会直接跳到答案页。两处问题由同课程 Group 2 首先发现（Lapis0x0/PE6203-cost-aware-harness），这里是独立重写。

离线自检（不需要容器和 API）：

```bash
python -m pytest tests/agents/test_tua.py tests/config/test_tua_configs.py -q
# Windows 下如果报 prompt_toolkit 找不到控制台：
python tua/scripts/pytest_noconsole.py tests/agents/test_tua.py tests/config/test_tua_configs.py -q
```

## 跑实验

`run_config.py` 会拼出完整的 `harbor run` 命令（`--dry-run` 只打印）。在 TUA-Bench 的 uv 环境里调用 harbor：

```bash
cd mini-swe-agent
H="uv run --project ../TUA-Bench harbor"

# 先用 1 个任务冒烟，确认容器、密钥、补丁都正常
python tua/scripts/run_config.py B0-qwen --tasks-dir ../TUA-Bench/tasks --harbor "$H" --only 034-turn-off-dark-mode
python tua/scripts/run_config.py A3-qwen --tasks-dir ../TUA-Bench/tasks --harbor "$H" --only 034-turn-off-dark-mode

# 正式：每个配置 40 题 × 3 次（--attempts 3）。机器弱就 --concurrency 1
for c in B0-qwen B1-dsflash A1-qwen A2-qwen A3-qwen A4-qwen A5-qwen H-qwen; do
  python tua/scripts/run_config.py $c --tasks-dir ../TUA-Bench/tasks --harbor "$H" --attempts 3 --concurrency 2
done
# 可选：A0-qwen（fork 全关的 sanity check）、H-dsflash
```

- 每题最长 2400 s（任务自带），40 题串行最坏约 27 小时，Qwen/DeepSeek 一般快得多。
- 中断后用 `harbor job resume jobs/<配置名>`（见 `harbor job resume --help`）继续，已完成的 trial 不会重跑。
- 输出在 `jobs/<配置名>/`：每个 trial 一个目录，含 `result.json`（reward）、`agent/mini-swe-agent.trajectory.json`（每步 usage）和 `verifier/`。
- 任何配置都不要手动改模型参数；要改请改 `tua/scripts/gen_configs.py` 后重新生成，并重跑所有受影响的配置。

## 汇总

```bash
python tua/scripts/summarize.py jobs/ --plot      # 需要 matplotlib 才画图
```

生成 `jobs/summary/summary.md`（每配置成功率、$/轮、harness 开销、token）、`per_task.csv`（逐题逐次）和 `pareto.png`。成本一律按 `tua/pricing/prices.json` 的固定价表乘 token 数计算，harness 开销 = 注入文本的 token（本 harness 不额外调用模型）。

把整个 `jobs/` 目录（或至少每个 trial 的 `result.json`、`agent/`、`verifier/`）打包发回，报告里的失败分类要读这些轨迹。

## 常见问题

- **容器里 `uv tool install` 失败 / 没网**：任务都允许联网；uv 需要下载 Python 3.12。失败就重跑该任务。
- **OpenRouter 400 "No endpoints found"**：固定供应商暂时不可用。等一会重试，不要改成允许 fallback（价格会变）。
- **验证器 reward 为空**：确认补丁已打（`patch_harbor.py --check`）。
- **Docker 内存**：子集只含 `cpus <= 2` 的任务，内存需求 2–8 GB，`--concurrency` 不要超过机器能承受的并发容器数。
- **想换模型**：在 `gen_configs.py` 的 `MODELS` 加一项、`prices.json` 加价格，重新生成配置。
