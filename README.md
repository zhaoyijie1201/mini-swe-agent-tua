# mini-swe-agent-tua

**mini-swe-agent 的成本感知改进版，面向 TUA-Bench。** NTU PE6203 Group Project 5（Cost-Aware Harness Optimization）。

本仓库 fork 自 [SWE-agent/mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent)（基于 v2.4.6，MIT 许可，作者 Kilian A. Lieret 与 Carlos E. Jimenez）。原版的介绍、文档和用法请看原仓库；这里只写我们改了什么、为什么改、怎么复现实验。

> A cost-aware fork of [mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent) v2.4.6 for [TUA-Bench](https://github.com/facebookresearch/TUA-Bench): five independently switchable harness improvements, ablation configs, a Harbor runner and an offline cost summarizer. All switches off reproduces upstream exactly.

## 要解决的问题

原版 mini-swe-agent 通过 Harbor 跑 TUA-Bench 时几乎是“裸跑”：没有步数、花费、时间上限（只有任务自带的 2400 s 硬杀），命令超时 30 s，观测只按字符数头尾截断，提交前不做任何检查，提示词面向 GitHub issue 修复而不是桌面 / 办公 / 浏览器任务。便宜模型在这种环境下的典型失败：

- 反复执行同一条命令原地打转；
- 被 dbus / GTK / chromium 的警告刷屏，上下文被噪声占满；
- 时间耗尽被硬杀，什么都没交；
- 交了一个空文件，或公式没算值、评分器读到空单元格的表格。

目标是：**让便宜模型（Qwen3.7 Flash）在不增加模型调用次数的前提下，用更少的钱接近昂贵模型（DeepSeek V4.1 Flash）在原版 harness 上的成功率。**

## 五个改进

每个改进是 `agent.*` 下的一个独立开关，默认关闭。**全部关闭时与上游 v2.4.6 行为逐条一致**（有测试保证），所以消融实验的差异只来自开关本身。

| 编号 | 开关 | 做了什么 |
|---|---|---|
| **C1 预算可见** | `budget_notices` | 每条观测末尾附 `[budget] step k/150 \| 已用时 \| 剩余时间 \| 剩余步数`；用掉 80% 预算时注入一次“请收尾并提交”。配套 `step_limit: 150`、`wall_time_limit_seconds: 2200`，给提交和验证留出时间 |
| **C2 停滞检测与恢复** | `stall_detection` | 同一命令在最近 6 步出现 ≥3 次、连续输出完全相同、命令超时，分别在观测后附一句提示；命令超时放宽到 120 s（LibreOffice、浏览器启动常超 30 s）；格式错误容忍从 3 次提到 10 次；无工具调用时给出区分“输出被截断”与“忘了调用工具”的恢复提示 |
| **C3 提交前自检门** | `verify_gate` | 第一次提交被拦截，在容器里跑一段只读 Python：任务里提到的路径是否存在 / 为空、任务开始后改了哪些文件、Office 文件能否作为 zip 打开、xlsx 公式是否缺缓存值。报告以 `<verification_report>` 交回模型，第二次提交放行。不读答案、不改任务文件 |
| **C4 观测去噪与压缩** | `observation_hygiene` | 删除已知噪声行、折叠重复行、按行数（头 80 / 尾 40）截断；发给模型时只保留最近 8 条观测全文，更早的只留前 400 字符。轨迹文件保存全文，不影响事后分析 |
| **C5 任务域提示词** | （配置）`system_template` / `instance_template` | 面向通用终端任务的提示词：先看环境再动手、改文件前备份、LibreOffice headless 重算并保存、GUI 用 `DISPLAY` + xdotool、像评分器一样自查、用指定命令提交。不含任何具体任务信息 |

C1–C4 实现为 `TuaAgent`（继承原版 `InteractiveAgent`），C5 只是模板替换。所有改进都不额外调用模型，harness 开销可以直接按注入文本的 token 计算。

## 实验设计

| 配置 | harness | 模型 | 用途 |
|---|---|---|---|
| `B0-qwen` | 原版 PyPI 2.4.6 | Qwen3.7 Flash | 便宜模型基线 |
| `B1-dsflash` | 原版 PyPI 2.4.6 | DeepSeek V4.1 Flash | 昂贵模型基线 |
| `A1`–`A5-qwen` | 本 fork，只开 C1…C5 之一 | Qwen | 单组件消融 |
| `H-qwen` | 本 fork，全开 | Qwen | 组合版（主结果） |
| `A0-qwen`、`H-dsflash` | 全关 / 全开 | Qwen / DeepSeek | 可选：fork 全关 ≈ 原版；增益是否随模型变强而缩小 |

- 所有配置共用一个固定的 40 题子集（`tua/configs/subset-40.json`，按类别分层，seed 6203，仅 `cpus <= 2` 的任务），每题跑 3 次。
- 模型走 OpenRouter，供应商固定（Qwen→Alibaba，DeepSeek→DeepSeek，禁止回退），Qwen 关闭 reasoning。
- 成本不看账单，按轨迹里的 token 数 × 固定价表（`tua/pricing/prices.json`，2026-10-30 标价）离线计算，输出成功率–成本散点与 Pareto 前沿。

## 相对上游新增了什么

```
src/minisweagent/agents/tua.py              TuaAgent：C1–C4 开关
src/minisweagent/agents/utils/tua_utils.py  去噪、截断、压缩、停滞检测、容器内检查脚本等纯函数
src/minisweagent/config/tua/base.yaml       与上游 mini.yaml 逐字相同
src/minisweagent/config/tua/prompt_tua.yaml C5 提示词
src/minisweagent/config/__init__.py         唯一改动的上游逻辑：配置查找多一个候选目录（一行）
tests/agents/test_tua.py                    开关行为与“全关 == 上游”的离线测试
tests/config/test_tua_configs.py            生成配置的端到端离线测试
tua/configs/                                10 份完整实验配置 + 40 题子集
tua/harbor_agent.py                         Harbor 自定义 agent：把本 fork 的 wheel 装进任务容器
tua/scripts/                                生成配置、生成子集、Harbor 补丁、运行、汇总
tua/README.md                               运行手册（环境、命令、续跑、汇总、FAQ）
tua/HANDOVER.md                             交接文档（改进点、进度、注意事项）
tua/PROPOSAL.md                             已批准的改进方案
```

上游代码除版本号（`2.4.6+tua.1`）、`.gitignore` 和上面那一行外没有改动，`git diff v2.4.6` 即全部改动。

## 快速开始

```bash
git clone https://github.com/zhaoyijie1201/mini-swe-agent-tua.git && cd mini-swe-agent-tua
python -m pytest tests/agents/test_tua.py tests/config/test_tua_configs.py -q   # 离线自检，不需要容器和 API
```

跑 TUA-Bench 需要 Linux / WSL2 + Docker、TUA-Bench 仓库和 OpenRouter 密钥，完整步骤（含 Harbor 两个补丁、构建 wheel、冒烟、正式运行、续跑、汇总）见 [`tua/README.md`](tua/README.md)。

## 当前状态

- C1–C5、配置生成、子集、Harbor agent、补丁脚本、运行与汇总脚本全部完成。
- 离线验证通过：Linux 247 个测试全过（含上游全套），Windows 20 过 1 跳过；全关 == 上游有测试保证；自检门脚本和 wheel 干净安装已验证。
- **尚未在真实容器 + 真实模型上运行**，实验结果待补。

## 致谢与许可

- 上游 [mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent)（MIT）。本仓库沿用 MIT 许可，原版权声明见 [`LICENSE.md`](LICENSE.md)。
- [TUA-Bench](https://github.com/facebookresearch/TUA-Bench)（CC BY-NC 4.0）。本仓库不包含任何任务内容，只含任务 ID；运行产生的轨迹会包含任务原文，请勿公开分发。
- [Harbor](https://github.com/laude-institute/harbor)（Apache 2.0）。本仓库不包含 Harbor 代码，补丁脚本在本机修改已安装的副本。
- Harbor 0.6.3 在 TUA 镜像下的两个问题（日志目录权限、`chrome_state.py` 泄题）由同课程 Group 2（[Lapis0x0/PE6203-cost-aware-harness](https://github.com/Lapis0x0/PE6203-cost-aware-harness)）首先发现，这里是独立重写。
