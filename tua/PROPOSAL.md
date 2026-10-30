# Project 5 · mini-swe-agent 改进方案（已批准，已实现）

> 实施状态（2026-10-30）：方案已在 `mini-swe-agent` 仓库的 `tua-harness` 分支实现，交付给同学的运行手册见 `tua/README.md`。下文为批准时的方案原文。

日期：2026-10-30（模型角色按当天讨论更新：DeepSeek V4.1 Flash 作为“昂贵模型”，再选一个更便宜的基座模型）。分工：我们只负责改进 harness 并交付可运行的包，实验由另一位同学在本地机器上跑；第 4、5 节是写给跑实验的同学看的。

调研依据：作业说明（docs/*.docx）、mini-swe-agent 源码（v2.4.6，已克隆到 `project2/mini-swe-agent`）、Harbor 0.6.3 源码（TUA-Bench 锁定的版本）、TUA-Bench 仓库、OpenRouter 实时模型/价格 API，以及同课程上一组（Lapis0x0/PE6203-cost-aware-harness，基于 Terminus-2）公开的失败分析。

---

## 0. 一页结论

1. **官方 benchmark 是 TUA-Bench，不是 SWE-bench。** 通过 Harbor 0.6.3 的 `harbor run -p tasks -a mini-swe-agent` 运行。Harbor 会在每个任务容器里 `uv tool install mini-swe-agent` 再执行 `mini-swe-agent --yolo -m ... -t <任务> --exit-immediately`。因此我们改过的 harness 要做成 **GitHub fork**，再写一个自定义 Harbor agent 类把 fork 装进容器。
2. **默认 harness 的真实状态**（在 Harbor 里）：单个 `bash` 工具调用、每步新 subshell、命令超时 30 s、无步数/成本/时间限制（Harbor 传了 `--cost-limit 0`）、只有 Harbor 的 2400 s 硬超时、观测超过 10k 字符截成头 5k+尾 5k、历史全量重发、连续 3 次“没有工具调用”就直接退出且无提交、提交前零检查、提示词全是 SWE-bench 口吻。这些正是作业列出的 5 个优化面。
3. **建议的改动，共 5 个开关（默认全关 = 原版），每个对应作业的一个优化面**：C1 预算可见与软限制、C2 停滞检测与恢复、C3 提交前自检门、C4 观测去噪与上下文压缩、C5 面向 TUA 的提示词。可选 C6：新增 `write_file` 工具。
4. **模型角色**：“昂贵模型”= `deepseek/deepseek-v4.1-flash-1017`（$0.15 / $0.60 每百万 token，固定 DeepSeek 供应商）；**基座“便宜模型”建议 `qwen/qwen3.7-flash`**（阿里首发供应商，$0.03 / $0.13，约为 Flash 的 1/5），备选 `anthropic/claude-haiku-5.5`（$0.10 / $0.50，工具调用最稳但差价小）或 `z-ai/glm-4.7-flash`（$0.061 / $0.40，仅第三方供应商）。
5. **为什么 Flash 适合当“昂贵”参照**：上一组在默认 Terminus-2 上测到 Flash 70.8%，高于 Claude Opus 5.5 的 62.0%（40 题、单次）。它既强又便宜，是一个很“硬”的目标；而作业的核心指标是成功率—成本平面上的 Pareto 前沿，不是单纯超过它的绝对成功率。

---

## 1. 两个必须先纠正的事实

### 1.1 benchmark 与接线方式

- 作业指定 TUA-Bench（120 题，Harbor 编排，执行式验证器），允许固定 ≥40 题子集。
- TUA-Bench `pyproject.toml` 锁定 `harbor==0.6.3`。TUA-Bench README 的官方命令形如 `uv run harbor run -p tasks -a terminus-2 -m ... -o jobs/... --yes`。
- Harbor 0.6.3 自带 `mini-swe-agent` 适配器（`harbor/agents/installed/mini_swe_agent.py`）。其行为：
  - 容器内以 root 装 `curl build-essential git`，再以 `agent` 用户装 uv 并 `uv tool install mini-swe-agent[==version]`（可用 `--ak version=2.4.6` 固定）。
  - 运行：`mini-swe-agent --yolo --model=<m> --task=<指令> --output=/logs/agent/mini-swe-agent.trajectory.json --cost-limit 0 [-c 自定义yaml] --exit-immediately`。
  - 支持 `--ak config_file=xxx.yaml`（把 yaml 写进容器并用 `-c` 传入）和 `--ak reasoning_effort=high`。
  - 跑完读取轨迹，统计 prompt/completion/cached token 和 litellm 估算成本，转成 ATIF 格式 `trajectory.json`。
- 因为 harness 装在容器里，**我们的改动必须能被 `uv tool install git+https://github.com/<我们>/mini-swe-agent@<tag>` 安装**。做法：写一个 `MiniSweAgentFork(MiniSweAgent)`，只覆盖 `install()`，用 `--agent-import-path` 指定。所有任务 `allow_internet = true`，容器里能拉 GitHub。

### 1.2 上一组的两条经验必须吸收

- **Harbor 0.6.3 的两个运行器补丁**（不改任务、不改验证器）：
  1. `chmod 777 /logs/agent /logs/verifier` 必须以 root 执行，否则 TUA 镜像（默认非 root 用户）下 chmod 静默失败，验证器写不出 reward。
  2. 浏览器类任务镜像里有 `/usr/local/bin/chrome_state.py`，其 `solved` 模式直接跳到答案页（给官方 oracle 用的），agent 能读能跑，会泄露答案。要在任务启动后删掉它。他们验证过：删掉后 oracle 得分从 1 变 0，Opus 的成绩从 67.0% 掉到 62.0%。
  建议我们**两条都采用并引用来源**，对所有配置一致应用。
- **噪声水平**：40 题单次运行的标准误约 ±7.6 个百分点，两次相同运行约有 1/5 的题结果翻转。所以单组件消融只能看“方向”，组合版才看显著性；成本够的话每个配置跑 3 次。

---

## 2. 默认 harness 在 TUA-Bench 下的逐项诊断

| 作业优化面 | mini-swe-agent 2.4.6 默认行为（`mini.yaml` + `InteractiveAgent(yolo)` + `LocalEnvironment`） | 问题 |
|---|---|---|
| 上下文管理 | 全部历史每步重发；观测 >10000 字符 → 头 5000 + 尾 5000 字符（`observation_template`）；无去噪、无压缩 | GUI/浏览器任务里 dbus、GTK、字体告警大量重复；长任务输入 token 线性膨胀；Flash 无 Anthropic 式缓存写入费，但输入仍计费 |
| 工具接口 | 只有一个 `bash(command)` 工具；每步 `bash -c` 新 subshell，cd/环境变量不持久；命令超时 30 s（`LocalEnvironmentConfig.timeout`） | 写文件只能靠 heredoc，引号嵌套易出错；30 s 对 LibreOffice 转换、pip 安装、Chromium 启动偏短，超时后只返回 `returncode -1` 无指导 |
| 预算分配 | `step_limit 0`、Harbor 传 `--cost-limit 0`、`wall_time_limit_seconds 0`；模型看不到已用步数/时间；唯一限制是 Harbor 的 2400 s 硬超时 | 跑飞的任务会一直花钱到 2400 s；被 Harbor 杀掉时没来得及写产物 |
| 失败检测与恢复 | 仅“连续 3 次响应无工具调用 → `RepeatedFormatError` 退出，提交为空”；无重复命令/相同报错检测 | 弱模型常用纯文本说“完成了”或反问，3 次即被判死刑，属于典型 harness 级过早终止；死循环无人打断 |
| 提交前验证 | `echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT` 即结束，零检查 | 最常见的静默失败：产物路径写错、xlsx 公式无缓存值、手写 OOXML 损坏、浏览器页面不是要求的页 |
| 提示词 | `mini.yaml`：“Please solve this issue…Create a script to reproduce the issue” | SWE 口吻，不提 GUI/`DISPLAY=:99`/xdotool/Office 文件/产物目录等 TUA 环境事实 |

成本统计方面：Harbor 设 `MSWEA_COST_TRACKING=ignore_errors`，用 litellm 价表估价；OpenRouter 同一模型有几十个供应商、价格不同，DeepSeek 官方端点还有分时段价格。**建议成本一律用轨迹里的 token 数 × 我们自己固定的价表离线计算**（作业也要求报告价表），OpenRouter 返回的美元数只作交叉核对。

---

## 3. 改进方案：5 个开关 + 1 个可选

设计原则：每项一个布尔开关，全关时行为与 PyPI 2.4.6 完全一致（写离线测试保证）；不含任何任务名、文件名、URL 或答案；harness 自身不额外调用模型，开销只来自注入的文本（便于报告“harness overhead”）。

### C1 预算可见与软限制（优化面：预算分配）
- 配置：`step_limit: 150`，`wall_time_limit_seconds: 2200`（留 200 s 给 Harbor 收尾，避免被硬杀）。
- 每条观测末尾附一行 `[budget] step 12/150 · elapsed 7m31s · remaining ~29m`。`observation_template` 已能拿到 `n_model_calls`、`elapsed_seconds` 等模板变量，基本是配置级改动。
- 剩余时间 <20% 时注入一次提醒：“把产物写到任务要求的位置并提交”。
- 预期：主要省钱、减少被硬杀而零产物的情况；成功率影响中性偏正。

### C2 停滞检测与恢复（优化面：失败检测与恢复）
- 最近 5 步内相同命令 ≥3 次，或相同报错输出 ≥3 次 → 注入“你在重复 X；请换思路/先读文档/检查前提”。
- 命令超时（returncode -1）→ 返回明确指导：用 `timeout N`、`nohup … &` + 轮询、或拆小步；命令超时从 30 s 提到 120 s（属于预算改动，会在报告中注明）。
- 格式错误恢复：模型纯文本回复时不再累计“三振出局”，而是回复“若已完成请执行提交命令；否则继续用 bash 工具”，`max_consecutive_format_errors` 提到 10。直接消除一类 harness 级过早终止。

### C3 提交前自检门（优化面：提交前验证）
- 第一次收到提交命令时拦截，harness 在容器里跑一段只读检查脚本并把结果回给模型：
  - 从任务陈述里抽取绝对路径，检查应产出的文件是否存在、非空；
  - 对 .xlsx/.docx/.pptx/.odt 等 Office 文件：zip 完整性、XML 格式良好性、xlsx 公式单元格是否缺少缓存值（LibreOffice 重算问题）；
  - 对图片/视频：可被 PIL/ffprobe 打开；
  - 附一个通用清单：“逐条重读任务要求，用命令验证每一条，再提交”。
- 模型修正后第二次提交直接放行（最多 1 次门，可配 2 次）。
- 上一组数据：这是单组件里最强的一项（+11/−2 题，p = 0.022）。它对 mini-swe-agent 同样适用，因为默认提交是零检查。

### C4 观测去噪与上下文压缩（优化面：上下文管理）
- 去噪：丢弃匹配通用噪声模式的行（dbus、Gtk-WARNING、Fontconfig、libEGL、GLib-GIO、Chromium 日志等），折叠连续重复行；截断改成按行（头 60 行 + 尾 40 行）并保留字符上限。
- 压缩：发给 API 时保留最近 K=8 条观测全文，更早的观测只留前 300 字符加 `[... N chars truncated]`；只改发送内容，轨迹文件仍保存全文。实现位置在模型层的 `_prepare_messages_for_api` 之前的一个钩子。
- 预期：输入 token 下降明显，成功率中性；会损失一部分 prompt cache 命中，需要用 token 账单核实净效果。

### C5 面向 TUA 的提示词（配置级，属于“工具接口/错误报告”的一部分）
- 系统/任务模板改写为通用的终端环境说明：Linux 容器、常见 `DISPLAY=:99` 与 xdotool、优先用无头 CLI（`libreoffice --headless --convert-to`、python 库）、改文件前先备份、不要手写 OOXML、改完重新打开产物核对、产物要写到任务指定的精确路径、任务陈述就是验收标准、如何提交。
- 不含任何任务特定信息。成本几乎为零，但要单独消融，因为它可能与 C3 有重叠收益。

### C6（可选，时间充裕再做）`write_file` 工具（优化面：工具粒度）
- 在 `BASH_TOOL` 旁新增 `write_file(path, content)`（可再加 `read_file(path, start, end)`），避免 heredoc 引号问题；参数校验失败时返回结构化错误。改动涉及 `actions_toolcall.py`、环境 `execute` 与观测格式化，工作量中等。

### 实施顺序建议
先做 C3、C2、C4（代码量最大、预期收益最大），C1 与 C5 基本是配置，顺手完成；C6 视进度。全部都做成开关后，一次性交给同学跑。

---

## 4. 实验矩阵与成本预估

| 配置 | 模型 | harness | 说明 |
|---|---|---|---|
| B0 | 便宜模型（Qwen3.7 Flash） | 原版 PyPI 2.4.6（`-a mini-swe-agent --ak version=2.4.6`） | 便宜模型基线 |
| B1 | DeepSeek V4.1 Flash | 原版 2.4.6 | 昂贵模型基线 |
| A1…A5 | 便宜模型 | fork，只开一个开关 | 单组件消融 |
| H | 便宜模型 | fork，全开 | 组合版 |
| （可选）H-ds | DeepSeek V4.1 Flash | fork，全开 | 回答“模型越强增益是否变小” |

- 子集：40 题，仅取 `cpus ≤ 2` 的任务（120 题中有 100 题满足，适合同学的个人电脑），按类别分层随机抽样，固定随机种子并写进配置文件；所有配置用同一子集。
- 时间：每题最长 40 min；40 题串行最坏约 27 h，Flash 通常远快于此。建议 `-n 1` 或 `-n 2`，视机器而定。
- 费用粗估（按 OpenRouter 标价，参考上一组的 token 量级：Flash 默认 harness 每轮 40 题约 $1.7）：DeepSeek Flash 每轮约 $1–4；Qwen3.7 Flash 单价是其 1/5，即便步数多一倍，每轮也约 $0.5–2。整个实验矩阵（8 个配置各 1 次）预计不超过 $20，所以**建议所有配置都跑 3 次**取均值，总花费仍在 $50 以内。
- 因为两个模型每轮都只有几美元，成功率—成本图的横轴会很窄，报告里同时给出 token 数，避免被供应商价格的小数位噪声误导。

---

## 5. 接线与运行方式（给跑实验的同学）

```bash
# 一次性准备
git clone https://github.com/facebookresearch/TUA-Bench && cd TUA-Bench
uv sync                      # 安装 harbor==0.6.3
uv run setup-env             # 下载任务资产
# 对 .venv 里的 harbor 应用两个运行器补丁（chmod as root；删除 chrome_state.py）
export OPENROUTER_API_KEY=...
```

```bash
# B0：便宜模型 + 原版 harness（-i 可重复多次，传入 40 个任务名）
uv run harbor run -p tasks -i 012-hide-na-budget-values -i 034-turn-off-dark-mode \
  -a mini-swe-agent --ak version=2.4.6 \
  -m openrouter/qwen/qwen3.7-flash \
  -e docker -n 1 -o jobs/B0-qwen --yes

# B1：昂贵模型（DeepSeek V4.1 Flash）+ 原版 harness，只换 -m 和 -o
uv run harbor run -p tasks -i ... -a mini-swe-agent --ak version=2.4.6 \
  -m openrouter/deepseek/deepseek-v4.1-flash-1017 -e docker -n 1 -o jobs/B1-dsflash --yes
```

```bash
# A3：便宜模型 + fork（只开 C3）
PYTHONPATH=../harness uv run harbor run -p tasks -i ... \
  --agent-import-path tua_harness.harbor_agent:MiniSweAgentFork \
  --ak fork_ref=v0.1 --ak config_file=../harness/configs/A3-verify-gate.yaml \
  -m openrouter/qwen/qwen3.7-flash \
  -e docker -n 1 -o jobs/A3-qwen --yes
```

- 模型通过 litellm 的 `openrouter/…` 前缀走 OpenRouter，Harbor 会自动把 `OPENROUTER_API_KEY` 传进容器。
- 供应商固定：在 yaml 里设 `model.model_kwargs.extra_body.provider: {order: ["Alibaba"], allow_fallbacks: false}`（Qwen）和 `{order: ["DeepSeek"], allow_fallbacks: false}`（DeepSeek Flash），避免 OpenRouter 在几十个第三方端点间漂移（部分端点甚至不支持 tools、价格也不同）。
- 解码与推理参数（temperature、reasoning 开关等）同一模型的所有配置完全一致，写死在 yaml 里并在报告中列出；不针对任何配置单独调参。
- 产出：`jobs/<配置>/<任务>/result.json`（reward）与 `agent/mini-swe-agent.trajectory.json`（每步 usage）。我们再提供 `scripts/summarize.py` 做 token × 价表 → 成功率—成本图、Pareto 前沿、消融表、成本拆分。

---

## 6. 模型选择

### 6.1 “昂贵模型”= DeepSeek V4.1 Flash，固定到带日期的快照

OpenRouter 上 “DeepSeek Flash” 对应 `deepseek/deepseek-v4.1-flash`（滚动版）、`deepseek/deepseek-v4.1-flash-1017`（2026-10-17 快照，DeepSeek 官方端点以 `deepseek-flash` 名字提供）和别名 `~deepseek/deepseek-flash-latest`。**固定用 `deepseek/deepseek-v4.1-flash-1017` + 供应商 DeepSeek**：标价 $0.15 / $0.60，缓存读 $0.003（每百万 token），上下文 1M，支持 tools。滚动版和 latest 别名在实验期间可能换模型，不利于复现；滚动版还有 40 多个第三方端点，价格从 $0.03 到 $0.45 不等，部分不支持 tools。

### 6.2 “便宜模型”候选：比 Flash 更便宜、支持工具调用、有首发供应商（OpenRouter 2026-10-30 标价，每百万 token）

筛选条件：输入 ≤ $0.15 且输出 ≤ $0.60、`supported_parameters` 含 tools、最好有模型厂商自己的端点（价格和行为稳定）。

| 模型 | 输入 / 输出 | 缓存读 | 相对 Flash 单价 | 供应商 | 评价 |
|---|---|---|---|---|---|
| **qwen/qwen3.7-flash**（推荐） | $0.03 / $0.13 | $0.006 | 约 1/5 / 1/4.6 | Alibaba（首发，唯一） | 2026-08 发布的小尺寸 Qwen3.7，支持 tools 与可选 reasoning，1M 上下文，输出上限 64k。TUA 榜单上 qwen3.7-max 都不如 deepseek-v4-pro，Flash 档几乎必然弱于 DeepSeek Flash，改进空间足够；单价差 5 倍，即使步数翻倍每轮成本仍远低于 Flash，Pareto 对比清晰 |
| anthropic/claude-haiku-5.5 | $0.10 / $0.50 | $0.01（缓存写 $0.125） | 约 2/3 / 5/6 | Anthropic（首发） | 工具调用最稳、浏览器类任务强，作为 Qwen 工具调用过于不稳时的保底；缺点是与 Flash 差价小，且可能并不弱于 Flash，“便宜模型”故事不鲜明 |
| z-ai/glm-4.7-flash | $0.061 / $0.40 | — | 约 2/5 / 2/3 | 仅 Cloudflare、Novita 第三方 | 30B 级、面向 agentic coding；无首发端点，可复现性略差 |
| openai/gpt-6-luna | $0.10 / $0.50 | $0.01 | 约 2/3 / 5/6 | OpenAI（首发） | 推理模型，思考 token 按输出计费，成本不易控；差价小 |
| qwen/qwen3.5-flash-02-23 | $0.065 / $0.26 | — | 约 2/5 / 2/5 | Alibaba（首发） | Qwen3.7 Flash 的上一代，没有理由选旧的 |
| openai/gpt-5-nano | $0.05 / $0.40 | $0.005 | 1/3 / 2/3 | OpenAI（首发） | 推理模型，agentic 能力弱，思考 token 不可控 |
| deepseek/deepseek-v4-flash-0731 | $0.119 / $0.238 | — | 4/5 / 2/5 | 仅第三方，价差极大 | 同系列旧版，差价小、端点乱，不建议 |

**结论**：基座便宜模型选 `qwen/qwen3.7-flash`（供应商固定 Alibaba）。它同时满足三个条件：明显更便宜（1/5 单价）、明显更弱（Qwen3.7 系列的小尺寸档）、工具调用由首发供应商提供。备选 `anthropic/claude-haiku-5.5`。推理开关建议：Qwen3.7 Flash 的 reasoning 在 yaml 里显式设置（建议关闭或 low，便于控制输出 token），DeepSeek Flash 沿用供应商默认；两者各自在所有配置中保持一致。

---

## 7. 需要你决定的事项

1. 便宜基座模型：`qwen/qwen3.7-flash`（推荐）还是 `anthropic/claude-haiku-5.5`？昂贵模型固定为 `deepseek/deepseek-v4.1-flash-1017`。
2. 是否批准 C1–C5 全做、C6 可选，并按 C3 → C2 → C4 → C1/C5 的顺序实现？
3. 是否采用上一组的两个 Harbor 运行器补丁（建议采用并引用）？
4. fork 放哪个 GitHub 账号/组织？容器要从公开仓库安装 harness。
5. 同学机器的 CPU/内存/系统（Docker Desktop + WSL2？），用于确定子集的 `cpus ≤ 2` 约束和并发数。

批准后我会交付给同学一个自包含的包：fork 分支（带开关与离线测试）、Harbor 自定义 agent 类与两个运行器补丁、每个配置一份 yaml、40 题子集文件、汇总脚本（token × 价表 → 成功率—成本图、消融表、成本拆分），以及一份运行手册。
