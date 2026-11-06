# WebAgent 开发协作说明（AGENTS.md）

> 适用范围：本仓库根目录及其所有子目录。
> 本文件供本地 Codex / AI 编码助手阅读；它是开发约束，不是已实现功能清单或未来功能需求书。

## 1. 项目定位与真实能力边界

- 当前基线：WebAgent v0.3，Python + Selenium 4 + Chrome + Streamlit。
- 六个 Agent 是**一个 Python 工程里的逻辑角色**，并非六个独立部署的模型服务。
- 当前主要支持 arXiv 高级搜索、结果列表与第一篇论文详情页的有限测试流程，另有本地 Fixture。
- 支持 `template`（无需 LLM API Key）和 `llm` 模式（DeepSeek / OpenRouter / OpenAI 兼容接口）。
- Agent1 尚不能规划任意动作序列；Agent2 的测试契约与预言仍面向既有任务；Agent3 主要通过定位器选择及固定执行模板生成脚本；Agent4 不是通用动作解释器。
- Agent5 是基于确定性规则的验证器，只支持有限证据补采；Agent6 只支持少量已知定位器错误的受控恢复。
- `PASS` 只说明该次运行中的已定义 Oracle 满足要求，不代表已具备通用 Web 缺陷发现能力。
- 故障注入演示与离线测试通过，不能被描述为真实网站上的自主发现和通用自愈。

## 2. 当前源码地图

| 位置 | 主要职责 |
| --- | --- |
| `run_demo.py` | CLI 入口 |
| `app_ui.py` | Streamlit 界面 |
| `web_agent/pipeline.py` | 六 Agent 流程编排、运行控制及反馈分支 |
| `web_agent/agents.py` | Agent1、Agent2、Agent5、Agent6 的核心决策类 |
| `web_agent/generator.py` | Agent3 的 DOM 定位方案与 Selenium 脚本生成 |
| `web_agent/selenium_runtime.py` | Agent4 的浏览器执行与主要取证操作 |
| `web_agent/dom_inspector.py` | 只读 DOM 信息采集 |
| `web_agent/evidence_plan.py` | 证据检查点与计划处理 |
| `web_agent/probe.py`、`web_agent/probe_runner.py` | 受控补证 |
| `web_agent/schema.py` | QuerySpec、TestContract、Verdict 等数据结构 |
| `web_agent/llm_adapter.py` | LLM 接入 |
| `web_agent/fixture_server.py` | 本地模拟页面 |
| `web_agent/report.py` | 报告生成 |
| `tests/` | 离线自动化测试 |
| `artifacts/` | 运行产物（一般不提交 Git） |

**不要因为文件名里有“Agent”就推断它具备完整自主推理或动态执行能力。**
当前 `TestContract.steps` 并非由 Agent4 通用逐条解释执行。

## 3. 代码修改原则

1. **先读后改**：修改前查阅相关模块、调用方、数据结构和测试；说明当前行为与预期变化。
2. **单任务、小范围修改**：除非明确要求，不同时重构六个 Agent，不借功能修复之机大规模搬迁文件。
3. **保留向后兼容**：未经明确授权，不改变 CLI 参数、Streamlit 主要流程、Test Contract/Oracle/Evidence Plan 的既有字段语义及四类状态 `PASS / FAIL / ERROR / INCONCLUSIVE`。
4. **职责边界**：Agent 负责自己的设计/审核/生成/执行/验证/恢复决策；Pipeline 统一编排；DOM、Selenium、LLM、Schema 等基础能力可以共享。不要为了“六 Agent 分开”复制六份基础设施。
5. **重构与功能改进分开提交**：纯结构重构应尽量保持行为不变；若修改行为，必须明确说明并增加回归测试。
6. **不夸大能力**：不把参数化流程描述为通用动态规划，不把受控定位器修复描述为通用自愈。
7. **不要执行任意不可信模型输出**：保留模型输出校验、定位器白名单、受控代码生成和明确的执行限制；不要把未经审查的 LLM 文本直接作为 Python/Shell 执行。
8. **敏感信息保护**：不要读取、打印、上传、提交或在报告里粘贴真实 API Key、`.env`、Streamlit secrets、凭证、Cookie 等信息；仓库只保留 `.env.example`。
9. **线上网站运行需明确授权**：默认先用 `fixture`/`fixture-bug`。访问真实 arXiv 时遵守现有的显式允许机制，未经用户要求不要启动真实网站测试、额外补证或自动重跑。
10. **本地资料**：`AGENTS.md` 和 `docs/` 已被 `.gitignore` 排除；不要假设它们存在于远程仓库。未经要求，不要删除或覆盖 `docs/` 内的阶段性汇报。

## 4. 测试与验证

优先使用已有项目测试；Windows PowerShell 示例：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

若当前环境未建立 `.venv`，在安装项目依赖后也可运行：

```powershell
python -m pytest -q
```

无需 LLM API Key 的模板模式演示（**运行 Selenium 时仍需要 Chrome / WebDriver 环境**）：

```powershell
python run_demo.py --target fixture --mode template
```

受控演示可用 `--demo-evidence-gap` 或 `--demo-broken-locator`，仅限本地 Fixture。
- 每次修复缺陷都应添加/更新对应的回归测试。
- 运行测试前后保持测试环境与生成产物可区分；运行产物写入 `artifacts/`，不要提交到仓库。
- 不得因先前记录为 45/45 通过，就声称此次修改后的测试也通过；必须提供本次实际执行结果。
- 区分静态检查、模拟/单元测试、本地 Chrome 端到端运行和真实 arXiv + LLM 运行；没有实际执行，就明确说“未验证”。

## 5. 每次任务完成时的交付说明

简要列出：
- 改了哪些文件及原因；
- 哪些行为保持不变，哪些行为发生改变；
- 执行了哪些检查/测试及其结果；
- 未验证的事项、现有风险和能力边界。

用户未要求实现新功能时，不应自行开展下一阶段开发。
