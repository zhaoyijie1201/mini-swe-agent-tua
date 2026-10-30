# PE6203 Project 5 交接文档：mini-swe-agent 成本感知 harness 改进

日期：2026-10-30
状态：harness 改进已完成并通过离线验证，**尚未在真实容器 + 真实模型上跑过**，改动**尚未提交**到 git。

配套文件：
- 改进方案（已批准）：`tua/PROPOSAL.md`
- 运行手册（给跑实验的同学）：`tua/README.md`
- 代码：本仓库，分支 `tua-harness`（从 v2.4.6 分出，版本号 `2.4.6+tua.1`）

---

## 1. 背景与分工

| 项 | 决定 |
|---|---|
| Harness | mini-swe-agent v2.4.6（tool-call 版，`mini` CLI） |
| Benchmark | TUA-Bench（120 道终端任务），通过 Harbor 0.6.3 运行 |
| 便宜模型 | Qwen3.7 Flash（OpenRouter `qwen/qwen3.7-flash`，供应商固定 Alibaba，关闭 reasoning） |
| 昂贵模型 | DeepSeek V4.1 Flash（OpenRouter `deepseek/deepseek-v4.1-flash-1017`，供应商固定 DeepSeek） |
| 三组对照 | ① 便宜模型 + 原版 harness；② 昂贵模型 + 原版 harness；③ 便宜模型 + 改进 harness |
| 任务子集 | 固定 40 题，所有配置共用 |
| 分工 | 本人负责改进 harness 并交付；另一位同学在自己有 Docker 的机器上跑实验 |

Harbor 的工作方式决定了交付形式：Harbor 在每个任务容器里用 `uv tool install` 安装 mini-swe-agent，所以改进版必须以 **wheel** 的形式被上传进容器，由一个自定义的 Harbor agent 类完成（见第 4 节）。

## 2. 设计原则

1. **开关化**：每个改进是一个独立开关（`agent.*` 配置项），默认关闭。全部关闭时与 PyPI 2.4.6 行为完全一致，有测试保证（`test_all_switches_off_matches_interactive_agent`）。这样消融实验（只开一个 / 全开）的差异只来自开关本身。
2. **只加不改**：除版本号、`.gitignore` 和 `config/__init__.py` 里多加一个候选目录（一行）外，原有文件没有改动；新功能都在新文件里。
3. **不额外调用模型**：所有改进都是 harness 侧的文本注入或拦截，不引入第二个模型，harness 开销可以直接按注入文本的 token 计算。
4. **不碰任务与评分**：自检门只做只读检查，不读答案、不改任务文件；两个 Harbor 补丁对所有配置一致，不改任务和验证器。
5. **成本可复现**：模型供应商固定且禁止回退，价格用固定价表（`tua/pricing/prices.json`，OpenRouter 2026-10-30 标价），成本 = token 数 × 价表，离线计算，与账单无关。

## 3. 改进点

原版 harness 在 Harbor 中的实际状态：没有步数、花费、时间上限（只有任务自带的 2400 s 硬杀），命令超时 30 s，观测只按字符头尾各 5000 截断，提交不做任何检查，提示词面向 GitHub issue 修复。便宜模型在这种环境下的典型失败：重复同一命令原地打转、被 GUI 噪声刷屏撑爆上下文、把时间耗尽后什么都没交、交了空文件或公式没算值的表格。

| 编号 | 开关 | 针对的问题 | 做了什么 | 配套参数 |
|---|---|---|---|---|
| C1 预算可见 | `budget_notices` | 模型不知道自己还剩多少步 / 时间，常在被硬杀前一无所交 | 每条观测末尾附 `[budget] step k/150 \| 已用时 \| 剩余时间 \| 剩余步数`；用掉 80% 预算（步数或时间）时注入一次 user 消息“请收尾并提交” | `step_limit: 150`，`wall_time_limit_seconds: 2200`（留 200 s 给提交与验证），`budget_wrapup_fraction: 0.8` |
| C2 停滞检测与恢复 | `stall_detection` | 同一命令反复执行、输出完全重复、命令超时后不知所措、格式错误 3 次就被终止 | 最近 6 步内同一命令出现 ≥3 次、连续相同输出、命令超时，分别在观测后附一句提示（不打断，不改历史）；放宽超时与格式错误容忍；无工具调用时给出更具体的恢复提示（区分“输出被截断”和“忘了调用工具”） | `stall_window: 6`，`stall_repeat_threshold: 3`，`environment.timeout: 120`（LibreOffice、浏览器启动常超 30 s），`max_consecutive_format_errors: 10`，`model.format_error_template` 替换 |
| C3 提交前自检门 | `verify_gate` | 模型自信地提交了不存在、为空或评分器读不出来的产物 | 第一次提交被拦截，harness 在容器里执行一段只读 Python：任务文本里提到的绝对路径是否存在 / 是否为空；任务开始后 `/app` 等目录改动了哪些文件；Office 文件能否作为 zip 打开、XML 是否完整；xlsx 公式单元格是否缺缓存值（openpyxl `data_only` 会读到空）。报告以 `<verification_report>` 返回给模型，第二次提交直接放行 | `verify_gate_max: 1`（只拦一次），`verify_gate_timeout: 90`；标记文件 `/tmp/.tua_task_start` 在 run 开始时 touch |
| C4 观测去噪与压缩 | `observation_hygiene` | dbus / GTK / fontconfig / chromium 的警告行刷屏；长输出按字符截断切坏行；早期的长观测一直占着上下文 | 去掉已知噪声行并折叠重复行（附 `[harness: N noise lines removed]`）；按行数头 80 / 尾 40 截断，上限 8000 字符；**发给模型时**只保留最近 8 条观测全文，更早的只留前 400 字符并附“已裁剪，需要可重跑命令”。轨迹文件保存全文，不影响事后分析 | `hygiene_head_lines: 80`，`hygiene_tail_lines: 40`，`hygiene_max_chars: 8000`，`compact_keep_last: 8`，`compact_head_chars: 400` |
| C5 任务域提示词 | （仅配置）`system_template` / `instance_template` | 原提示词讲的是修 GitHub issue，和 TUA 的桌面 / 办公 / 浏览器任务不匹配 | `config/tua/prompt_tua.yaml`：面向通用终端任务的规则——先看环境再动手、改文件前备份、LibreOffice 用 headless 重算并保存、GUI 任务用 `DISPLAY` + xdotool、像评分器一样自查、用指定命令提交。不含任何具体任务信息 | 无 |

C1–C4 实现在 `TuaAgent`（继承 `InteractiveAgent`，Harbor 用的就是这个类）；C5 只是模板替换。

## 4. 交付物清单

全部在本仓库，分支 `tua-harness`。

| 路径 | 内容 |
|---|---|
| `src/minisweagent/agents/tua.py` | `TuaAgentConfig` + `TuaAgent`：四个代码开关；`_CompactingModel` 代理在发给模型前压缩旧观测 |
| `src/minisweagent/agents/utils/tua_utils.py` | 纯函数：去噪、按行截断、压缩、命令归一化与输出指纹、提示文案、容器内检查脚本与其 bash 包装、预算行、收尾提醒 |
| `src/minisweagent/config/tua/base.yaml` | 与 `mini.yaml` 逐字相同（原版配置，有测试保证） |
| `src/minisweagent/config/tua/prompt_tua.yaml` | C5 提示词 |
| `src/minisweagent/config/__init__.py` | 唯一改动的原有逻辑文件：配置查找多一个候选目录 `config/tua/`（一行） |
| `src/minisweagent/__init__.py` | 版本号 `2.4.6+tua.1` |
| `tua/configs/*.yaml` | 10 份完整实验配置（见第 5 节），由脚本生成，**不要手改** |
| `tua/configs/subset-40.json` | 固定 40 题子集 |
| `tua/harbor_agent.py` | `MiniSweAgentFork(MiniSweAgent)`：Harbor 自定义 agent，把本地 wheel 上传进容器后 `uv tool install` |
| `tua/scripts/gen_configs.py` | 生成配置矩阵（base + agent_class + 开关叠加 + 模型） |
| `tua/scripts/make_subset.py` | 生成子集（seed 6203，可复现） |
| `tua/scripts/patch_harbor.py` | 给已安装的 Harbor 0.6.3 打两个补丁，幂等，`--check` 可验证 |
| `tua/scripts/run_config.py` | 拼出并执行某个配置的 `harbor run` 命令（`--dry-run` 只打印） |
| `tua/scripts/summarize.py` | 从 `jobs/` 读原始结果，算成功率、token、美元、harness 开销，输出 summary.md / per_task.csv / pareto.png |
| `tua/scripts/build_wheel.sh`、`pytest_noconsole.py` | 构建 wheel；Windows 下绕过 prompt_toolkit 无控制台报错跑测试 |
| `tua/pricing/prices.json` | 固定价表（USD / 1M token） |
| `tua/dist/mini_swe_agent-2.4.6+tua.1-py3-none-any.whl` | 已构建的 wheel（被 `.gitignore` 忽略，同学需自行构建或拿这个文件） |
| `tests/agents/test_tua.py`、`tests/config/test_tua_configs.py` | 离线测试，无容器、无模型调用 |
| `tua/README.md` | 给跑实验同学的运行手册（环境、命令、续跑、汇总、FAQ） |

两个 Harbor 补丁（同课程 Group 2 首先发现，这里独立重写）：
1. Harbor 对日志目录 `chmod 777` 改为以 root 执行并包含 artifacts 目录，否则 TUA 镜像（默认非 root 用户）下验证器写不出 reward。
2. 任务容器启动后删除 `/usr/local/bin/chrome_state.py`，它的 `solved` 模式会直接把浏览器跳到答案页，相当于泄题。

## 5. 实验设计

### 5.1 配置矩阵

| 配置 | harness | 模型 | 用途 |
|---|---|---|---|
| `B0-qwen` | 原版 PyPI 2.4.6（Harbor 自带 agent） | Qwen3.7 Flash | 便宜模型基线 |
| `B1-dsflash` | 原版 PyPI 2.4.6 | DeepSeek V4.1 Flash | 昂贵模型基线 |
| `A0-qwen` | fork，全关 | Qwen | 可选 sanity check：应 ≈ B0 |
| `A1-qwen` … `A5-qwen` | fork，只开 C1 … C5 之一 | Qwen | 单组件消融 |
| `H-qwen` | fork，全开 | Qwen | 组合版（主结果） |
| `H-dsflash` | fork，全开 | DeepSeek Flash | 可选：增益是否随模型变强而缩小 |

必跑 8 个：B0、B1、A1–A5、H-qwen。每个配置 40 题 × 3 次（`--attempts 3`），单次运行 40 题的标准误约 ±7.6 个百分点，3 次能把误差条压到可区分的程度。

### 5.2 子集

`subset-40.json`：TUA-Bench 提交 `3497fd320abcafaf4797424192c891a593fd7964`，只取 `cpus <= 2` 的任务（120 题中有 100 题），按类别分层、最大余数法分配配额、`random.Random(6203)` 组内抽样。配额：browser 9，presentation-editing 5，spreadsheet-editing 5，image-editing 3，os 3，writer 3，calc 2，chrome 2，document-editing 2，developer-tools / email / gimp / media-editing / thunderbird / vscode 各 1。

### 5.3 成本

- 从每个 trial 的 `agent/mini-swe-agent.trajectory.json` 读每步 usage（prompt / cached / completion token），乘 `prices.json`。
- harness 开销 = 注入文本（`<harness_note>`、`<verification_report>`、收尾提醒）的 token，`summarize.py` 自动统计。
- 两种模型都走 OpenRouter，`extra_body.provider.order` 固定供应商、`allow_fallbacks: false`；Qwen 关闭 reasoning。同一模型的所有配置模型参数完全一致。

### 5.4 预期的分析角度

- 成功率 vs 美元 / 轮散点图，Pareto 前沿（`summarize.py --plot`）。
- 每个组件的边际贡献（A1–A5 相对 B0），以及组合是否叠加（H 相对各 A）。
- H-qwen 能否以远低于 B1 的成本接近或超过 B1。
- 失败分类要读轨迹：超时 / 停滞 / 产物缺失 / 内容错误，对比 B0 与 H 的分布变化。

## 6. 目前进度

### 已完成并验证

| 项 | 状态 |
|---|---|
| 方案 | 已批准（`tua/PROPOSAL.md`） |
| C1–C5 实现、配置生成、子集、Harbor agent、补丁、脚本、README | 完成 |
| Linux 测试（WSL，Python 3.12） | 247 个全部通过，含上游 `tests/agents`、`tests/config` 全套和 Linux 专用超时提示测试 |
| Windows 测试 | tua 相关 20 个通过、1 个跳过（超时测试仅 Linux） |
| 全关 == 原版 | 有测试：相同模型输出下 `TuaAgent`（全关）与 `InteractiveAgent` 消息逐条相等 |
| 自检门脚本 | Linux 上对伪造 xlsx / docx 正确报告缺失文件、空文件、公式缺缓存值、损坏 docx |
| wheel | 已构建，干净 venv 安装后版本、CLI、`TuaAgent` 导入、两份 tua 配置均正常 |
| Harbor agent 类 | 能在 harbor 0.6.3 下导入；`run_config.py --dry-run` 生成的命令正确 |
| `patch_harbor.py` | 在本机 venv 的 harbor 0.6.3 上验证可打补丁且幂等 |
| ruff | check 与 format 全部通过 |

### 未完成

- **未在真实容器 + 真实模型上跑过任何一题。** 本机 WSL 没有 Docker。接手后第一步是按 README 用 `--only 034-turn-off-dark-mode` 对 B0 和 A3 各冒烟 1 题，确认容器能装 wheel、密钥可用、补丁生效、自检门能在容器里跑通。
- 改动都在工作区，**未 commit**（`git status` 见 3 个修改文件 + 若干新文件）。
- fork 放哪个 GitHub 仓库未定。因为走的是 wheel 上传，容器不需要访问 GitHub，公开仓库不是必需；但同学要拿到代码，至少要有一个私有仓库或直接打包目录。
- 同学机器配置未知。子集只含 `cpus <= 2` 的任务，内存需求 2–8 GB / 容器，`--concurrency` 按机器定。

## 7. 接手步骤（概要，细节见 `tua/README.md`）

1. clone 本仓库（分支 `tua-harness`）和 `TUA-Bench` 仓库（checkout 到上述提交）。
2. 在 TUA-Bench 目录 `uv sync && uv run setup-env`，得到带 harbor 0.6.3 的环境并下载任务资产。
3. 用该环境运行 `tua/scripts/patch_harbor.py`，再 `--check` 确认。
4. 在 mini-swe-agent 目录构建 wheel：`python -m pip wheel --no-deps -w tua/dist .`（或直接用已有的 `tua/dist/*.whl`）。
5. 设 `OPENROUTER_API_KEY`，先跑离线测试，再冒烟 1 题，再正式跑 8 个配置。
6. 中断用 `harbor job resume jobs/<配置名>` 续跑；全部跑完 `python tua/scripts/summarize.py jobs/ --plot`。
7. 把整个 `jobs/` 打包发回，写报告时要读轨迹做失败分类。

国内网络直连 PyPI 很慢时，给 uv 加 `UV_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple`（本机 WSL 就是这样装好的）。

## 8. 注意事项

- 不要手改 `tua/configs/*.yaml`。要改参数就改 `gen_configs.py` 重新生成，并重跑所有受影响的配置，否则对照失效。
- 不要把 `allow_fallbacks` 改成 true：供应商变了价格就变了，成本不可比。
- 原版基线 B0 / B1 用的是 Harbor 自带的 `mini-swe-agent` agent（PyPI 2.4.6），不是本 fork；这是刻意为之，保证“原版”真的是原版。
- `verify_gate` 的检查只验证“文件存在且能打开”，不验证内容正确；报告里也这样写，不要夸大。
- 任何一个开关只在自己的 A 配置里出现；C2 的配套参数（超时、格式错误容忍、恢复提示）算作 C2 的一部分，一起开关。
