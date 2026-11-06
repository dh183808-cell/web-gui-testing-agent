# WebAgent v0.4 — Selenium Web GUI Testing Agent

> **Six logical roles**, with a working evidence collection plan, targeted re-observation and bounded test-script recovery. Keeps DeepSeek / OpenRouter / OpenAI support from v0.2.2.
>
> **Scope**: v0.3 兼容的 arXiv-like 流程，以及 v0.4 受审核语义契约驱动的本地动态流程。这是受限工程原型，不是通用 6-LLM-agent 浏览器平台。

v0.4 动态流程将自然语言需求冻结为 `TestDesign/DynamicTestContract`，由 Agent3 根据每次实时 DOM 生成封闭 Selenium 操作，Agent4 在同一浏览器会话执行，Agent5 验证动态证据，Agent6 只对未开始且安全的定位/等待错误做有限恢复。动态步骤成功但缺少必要 Oracle 证据时返回 `INCONCLUSIVE`，不会伪造 `PASS`。

## 1. Windows PowerShell: installation

Clone or open this repository. Its root folder is `WebAgent`.

```powershell
cd .\WebAgent
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple --timeout 120 --retries 5
.\.venv\Scripts\python.exe -m pytest -q
```

No PowerShell activation is necessary; always run the project's `.venv` Python to avoid accidentally invoking Anaconda's packages. Python 3.11/3.12 and Chrome are recommended. If the mirror is unavailable, remove the `-i ...` option.

## 2. Three demonstrations (no API key needed)

**Demo A — Normal full workflow (expected PASS)**

```powershell
.\.venv\Scripts\python.exe run_demo.py --target fixture --mode template --show-browser --demo-delay 0.7
```

**Demo B — Agent5 active evidence acquisition (expected PASS after one probe)**

```powershell
.\.venv\Scripts\python.exe run_demo.py --target fixture --mode template --demo-evidence-gap --show-browser --demo-delay 0.7
```

Local-only *controlled fault injection*: the recorded `detail.title` is deliberately omitted after the first browser execution; Agent5 identifies the missing oracle evidence and requests Agent4 to **open that same paper detail in a fresh Selenium browser session** and read only the missing field. Inspect `controlled_evidence_gap.json`, `probe_request.json`, `probe_result.json`, `collection_log` and `report.html`. This is an **evidence collection simulation**, not a discovered arXiv bug.

**Demo C — Agent6 bounded locator recovery (expected PASS after first ERROR and one repair)**

```powershell
.\.venv\Scripts\python.exe run_demo.py --target fixture --mode template --demo-broken-locator --show-browser --demo-delay 0.7
```

Another *controlled fault injection*: the initial Selenium script is generated with a broken keyword locator. Agent6 classifies the `locate_keyword` error, requests **one allowlisted locator-only regeneration** from Agent3, and Agent4 executes the new script. Inspect `repair_request.json`, `generated_test_attempt2.py`, `evidence_attempt2.json`, `recovery_history.json` and the report. The original test contract/oracles do not change.

**Demo D — Known faulty application (expected FAIL, no automatic repair)**

```powershell
.\.venv\Scripts\python.exe run_demo.py --target fixture-bug --mode template --show-browser
```

The injected bug deliberately changes a search-list title but not its matching detail title. **FAIL is expected**; the CLI exit code is `1` because the test failed. The system must not rewrite a correct oracle to turn this into PASS.

You may disable active mechanisms in the CLI using `--max-probes 0` and/or `--max-repairs 0`. v0.3 permits at most one targeted probe and one code repair per run. The fault-injection flags are accepted *only for local fixtures*.

## 3. Streamlit demo

```powershell
.\.venv\Scripts\python.exe -m streamlit run app_ui.py
```

The UI exposes the target, template/LLM mode, local fault injection, probe/repair budgets, actual screenshots, generation audit, acquisition logs and recovery decisions. The screenshot tab is for viewing evidence, **not** a real-time desktop mirror. Use `--show-browser` or uncheck the headless option to show Selenium driving Chrome.

### v0.4 动态模式

```powershell
# 需要真实模型配置：自然语言 → Agent1 规划 → Agent2 审核 → 浏览器执行
python run_demo.py --engine dynamic --fixture-site catalog --mode llm --execute-dynamic --requirement '搜索“机器人”，分类选择“机器人”，打开第二项，确认结果标题与详情标题一致。'

# 无 Key：使用已编写的语义设计，真实 Chrome 执行下载流程
$design = Get-Content examples/download_design.json -Raw -Encoding UTF8 | ConvertFrom-Json
python run_demo.py --engine dynamic --fixture-site download --mode template --design-file examples/download_design.json --requirement $design.requirement --allow-downloads --execute-dynamic
```

| 选项 | 含义 |
| --- | --- |
| `--engine legacy`（CLI 默认） | 原 v0.3 搜索流程，原参数保持兼容 |
| `--engine dynamic` | v0.4 语义步骤、持久会话、动态验证和恢复 |
| `--mode template` | 旧模式使用旧模板；动态模式必须提供 authored `--design-file`，不解释任意自然语言 |
| `--mode llm` | 使用现有 LLM Adapter；动态 Agent1 规划、Agent2 语义审核、Agent3 实时 DOM 定位 |
| `--fixture-site catalog/form/download` | Pipeline 启动并拥有的本机合成页面；不连接线上网站 |
| `--target-url http://127.0.0.1:端口/路径` | 用户已启动的不同本地网站，不需要改后端 Python |
| `--execute-dynamic` | 显式执行；省略则仅规划，返回 NOT_RUN / INCONCLUSIVE |
| `--allow-live-site` | 外部 URL 执行必须明确许可；即使授权也只允许起始 URL 的精确 origin（含端口） |
| `--enable-live-probes / --enable-live-recovery` | 外部站点额外补证/恢复的独立许可，默认关闭 |
| `--allowed-actions ...` | 限制契约可执行动作，在创建浏览器前检查 |
| `--allow-downloads` | 仅开启契约声明的同源下载，默认关闭 |

模板 JSON 的 `target_url` 可写 `$TARGET_URL` 绑定临时 Fixture 地址，其他语义不改写；`requirement` 必须与提交原文一致。Streamlit 提供同样的引擎、目标、模型、模板 JSON、动作及权限选项，并展示设计、审核、逐步代码、DOM/定位审计、执行状态、证据、Oracle 和补证恢复历史。界面初始为兼容模式；动态模式不是实时桌面监控。

每次运行写入 `artifacts/<run_id>/`。动态报告是 `dynamic_report.html`，完整结构化结果是 `dynamic_result.json`；每个执行尝试的代码/审计在 `generation/<step>_attemptN/`，证据在 `dynamic_evidence.json`，轨迹在 `execution_attempts.json`。报告支持追踪需求 → 步骤 → Oracle → 源码摘要 → Observation → Evidence → 裁决，页面文字和代码在报告中转义。

## 4. Real DeepSeek / OpenRouter LLM

Use the **same `.env.example` and LLM adapter as v0.2.2**. DeepSeek and OpenRouter use OpenAI-compatible API endpoints, with their *own* provider credentials (not an OpenAI API key). Create a `.env` and never commit it or include it in screen recordings.

```powershell
Copy-Item .env.example .env
notepad .env
```

For DeepSeek, fill in `LLM_PROVIDER=deepseek`, `DEEPSEEK_API_KEY=...`, `DEEPSEEK_MODEL=...`. Use a model ID supported by your account/provider. For OpenRouter, fill in `LLM_PROVIDER=openrouter`, `OPENROUTER_API_KEY=...`, `OPENROUTER_MODEL=provider/model` (replace it with an available model ID).

```powershell
.\.venv\Scripts\python.exe run_demo.py --target fixture --mode llm --provider deepseek --show-browser
# or: --provider openrouter
```

LLM mode uses the actual model for Agent1's bounded query intent, Agent2's independent review and Agent3's locator ranking. Agent4 remains deterministic Selenium execution; Agent5 and Agent6 have **real decision logic**, but are *not* independent LLM instances. This design avoids running arbitrary generated Python from untrusted model output.

## 5. What changed in v0.3 and v0.4?

| Role | v0.3 execution | Main output |
|---|---|---|
| Agent1 | Parse bounded natural language search intent | `draft_cases.json` |
| Agent2 | Review intent, freeze oracles, issue `Evidence Plan` (4 checkpoints for a full search/detail run) | `test_contract.json` |
| Agent3 | Read current DOM using DOM Inspector, select allowlisted locators, render reviewable Selenium Python; regenerate limited locators on approved repair | `generated_test.py`, `generated_test_attempt2.py` if needed |
| **Agent4** | Selenium GUI operations; execute evidence checkpoints, selectively take requested screenshots, preserve acquisition logs; execute independent detail re-observation | `evidence.json`, `collection_log`, `probe_result.json` |
| **Agent5** | Check oracles; identify *missing* eligible evidence; issue a bounded `detail_reobserve` request, then re-evaluate | `probe_request.json`, revised verdict |
| **Agent6** | Diagnose bounded locator errors (keyword/field/CS control); request one regenerating pass; route terminal results and keep history | `repair_request.json`, `recovery_history.json`, `final_verdict.json`, `report.html` |

Agent4's collectors are **plan-driven for the supported search workflow**, not an arbitrary-evidence DSL; only allowlisted fields/checkpoints are available. Agent5's interactive verification is currently restricted to missing *paper detail title/ID* evidence. Agent6 can repair a subset of locator failures, not timeouts due to network, CAPTCHA or application failures.

The v0.4 dynamic path uses a versioned evidence envelope with step/evidence/oracle provenance, generic text/number/state/URL/file relations, read-only same-checkpoint probes, and a fresh-DOM recovery decision. The v0.3 fixed field allowlist remains unchanged for legacy runs. `docs/` and `AGENTS.md` are locally ignored; the repository README and tests provide the reproducible public instructions.

### v0.3 execution data flow

```
Requirements → Agent1 → Agent2: Test Contract + Evidence Plan
                                    │
                                    ▼
                       Agent3: DOM Inspector + script
                                    │
                                    ▼
                         Agent4: Selenium + evidence
                                    │
                                    ▼
                         Agent5: oracle verification
                           │                 │
                 missing detail data    execution ERROR
                           │                 │
                   targeted probe     Agent6 repair decision
                           │                 │
                   Agent4 reobserve   Agent3 regenerate → Agent4 rerun
                           └────────┬────────┘
                                    ▼
                           final HTML report
```

All original and repaired script/source files are retained. Each run has its own timestamped `artifacts/<run_id>/` folder, and `report.html` includes checkpoint observations, probes and repair history.

## 6. Targeting real arXiv

The baseline arXiv Selenium run has been demonstrated by the user on **v0.2.2**. **v0.3 has not been executed against arxiv.org in this build environment.** The site may change, require consent or block automated requests. `--target arxiv` therefore requires `--allow-live-site` and the operator's confirmation of acceptable usage.

Example *only when you have permission*:

```powershell
.\.venv\Scripts\python.exe run_demo.py --target arxiv --allow-live-site --mode llm --provider deepseek --show-browser
```

By default, the real target **does not** automatically trigger additional probe or repair visits, even though local fixtures can. To explicitly permit one extra visit / test rerun under appropriate authorization, use `--enable-live-probes` and/or `--enable-live-recovery`. Do not circumvent access controls, login requirements or rate limits. This tool is for low-frequency, permitted testing, not scraping.

## 7. Supported / unsupported

- 已实现：旧版 arXiv-like 流程；动态冻结契约、原生控件动作、单 origin / 单窗口持久 Chrome、有限状态/文本/数量/URL/文件 Oracle、只读补证、有限恢复、UI/CLI/HTML 报告。
- 已测试：Mock LLM 的规划/审核/定位与真实本机 Chrome 联合运行，搜索筛选与导航、异步多步骤表单、文件下载、ID/层级/按钮文字变化、不同测试数据及缺失控件；单元/协议/模拟回归。可访问名称在布局变体中保留，因此不证明名称任意变化的泛化。
- 部分实现：有限原生控件、精确 aria-label 缺失判断、文件完成/大小/摘要/基础格式校验；不做任意文件业务内容理解。
- 未验证：真实 LLM + 浏览器、线上网站和真实站点授权后的可用性；测试不访问外部网站，不要求 API Key。
- 未实现：通用登录/支付、跨 origin 导航、iframe/Shadow DOM 通用处理、复杂拖拽控件、万能恢复、任意 LLM Python 执行或视觉缺陷判断。
- `PASS` 必须有可信证据支持全部必要 Oracle；确认 `FAIL` 优先于其他缺证；执行错误不是应用 Bug。模板/只规划不能被描述为自然语言真实模型验收。
- 不自动重放已经开始的写入、点击、提交、下载；只读补证不返回历史页面。代码安全依靠封闭操作和 Worker 源码二次校验，子进程不是任意 Python 的 OS 沙箱。

## 8. Verification status

运行默认离线集合（不启动 Chrome）与显式本地浏览器验收：

```powershell
python -B -m pytest -q -p no:cacheprovider
$env:WEBAGENT_REAL_CHROME='1'
# 若 Selenium Manager 无法联网，可将 CHROMEDRIVER_PATH 设为本机匹配版本的驱动绝对路径
python -B -m pytest -q -p no:cacheprovider --basetemp artifacts/new_acceptance_run --junitxml artifacts/acceptance.xml
```

`--basetemp` 必须选新目录，pytest 会清理该目录。测试分为 Python 单元、Mock LLM + Pipeline、真实 Chrome + 本机 Fixture、真实 LLM + 浏览器；最后一类本次未执行。

2026-11-06 本次最终实测：默认集合 **225 passed、17 skipped（10.98 秒）**；启用本地真实 Chrome 后完整集合 **242 passed（172.50 秒，无跳过）**，包含 **17 项真实 Chrome 测试**。Chrome 156.0.8078.74、ChromeDriver 156.0.8078.80、Python 3.12.3、Selenium 4.36.0。Streamlit AppTest 实测版本为本机已安装的 1.32.0，未验证按 requirements 中 >=1.40 全新安装的环境。浏览器测试使用 Mock LLM 或显式编写的语义设计，不证明真实模型的自然语言泛化。历史 v0.3 的 45 项通过不能代替本次结果；本地日志、JUnit 和审计摘要保存在 `artifacts/v04_stage4_acceptance_20261106/`，该目录不提交 Git。
