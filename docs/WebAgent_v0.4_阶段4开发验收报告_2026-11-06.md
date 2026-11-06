# WebAgent v0.4 开发与验收报告（阶段 4/4）

日期：2026-11-06。范围：六 Agent 完整数据流、动态目标配置、Streamlit/CLI 集成、动态 HTML 报告与本地跨场景验收。未提交、推送、创建 Git 标签，未在本阶段访问外部受测网站。

## 1. 基线与总体判断

开发前阅读了本地被忽略的 AGENTS.md、app_ui.py、run_demo.py、pipeline.py、report.py、fixture_server.py、dynamic_fixture.py、六 Agent 最新接口及现有测试。HEAD 为 `fe4a76dc8f41dc532aecc7734799c8e5fc74b04f`。工作区已有前三阶段未提交修改，本阶段保留并在其上整合。

开始时默认完整集合实测 **213 passed、11 skipped，6.31 秒**。跳过的是需要显式启用 Chrome 的用例，不代表浏览器验收失败。最终测试数字与逐项证据见本文后续记录及本机 `artifacts/v04_stage4_acceptance_20261106/pytest.xml`。

客观结论：v0.4 的受限动态测试工程链路已经贯通，具备可审查语义契约、实时 DOM 定位、持久 Selenium 执行、来源证据、确定性 Oracle、有限恢复以及 UI/CLI/HTML 交付能力。**本地 Chrome + Mock LLM 的验收可以支持这些工程结论，但不能证明真实模型已经具备任意网站自然语言泛化能力。** 真实 LLM + 浏览器验收保留待人工配置和授权；本次未读取或使用真实 API Key。

## 2. v0.3 与 v0.4 架构比较

| 维度 | v0.3 兼容路径 | v0.4 动态路径 |
| --- | --- | --- |
| 任务 | arXiv-like 受限参数化搜索 | 原始需求、语义步骤、依赖、数据与 Oracle |
| 规划 | QuerySpec / 固定流程 | 独立 TestDesign → 审核冻结 DynamicTestContract |
| Agent3 | 允许列表选择器与固定搜索模板 | 当前步骤 + 实时 DOM → 封闭操作 → 动作相关 Python 源码 |
| Agent4 | 旧脚本/补证各自管理浏览器 | 单个持久 Worker 独占 Chrome，观察与执行共享会话 |
| 验证 | 既有搜索字段和 Evidence Plan | Evidence ID 及来源封装，关系注册表，跨步骤/页面比较 |
| 补证/恢复 | 已知详情字段/定位器受控重跑 | 当前只读检查点补证、新 DOM 有界恢复，禁止重放已开始写操作 |
| 目标 | fixture / fixture-bug / arxiv | 内置三种本地目标或用户指定 URL；精确同 origin |
| 展示 | 原 HTML 报告与旧结果页 | 动态完整结果、源码/定位审计、DOM、证据及历史；保留旧展示 |

仍然是一个 Python 工程里的六个逻辑角色，没有新增平行多 Agent 框架，也没有把六角色拆成独立模型服务。旧 `web_agent.generator` 和 `selenium_runtime.execute_test()` 继续保留。

## 3. 六 Agent 接口、协议与所有权

| 角色 | 输入与主要接口 | 输出/边界 |
| --- | --- | --- |
| Agent1 | `design(..., engine='dynamic', target_url, mode, llm)` | 原始文本生成 v0.4 TestDesign；不生成 CSS/XPath/Python |
| Agent2 | `review_dynamic(design, requirement, target_url, mode, llm)` | 确定性协议/覆盖检查与 LLM 语义审核；最多两轮；批准后冻结摘要 |
| Agent3 | `generate_dynamic(contract, step_id, observation, run_dir, state, policy, mode, llm)` | 绑定步骤/Observation、节点依据、等待/变化、真实 Python 与审计；不持有 WebDriver |
| Agent4 | `open_dynamic()` → `observe/execute/probe/close` | Worker 独占 WebDriver 和真实节点引用；JSON IPC；采集证据和结构化错误 |
| Agent5 | `verify_dynamic()`、`plan_dynamic_probe()` | 只消费冻结 Oracle，按 Evidence ID 比较；缺证请求关联原 Oracle/步骤 |
| Agent6 | `plan_dynamic_repair()` | 只判断是否可用新 DOM 改变执行方法；不更改目标、值或 Oracle |
| Pipeline | `run_pipeline(engine='dynamic', execute_dynamic=True, ...)` | 拥有 Fixture、预算、会话生命周期、逐检查点验证、补证/恢复调度和报告 |

语义协议 `protocol_version=0.4`；运行证据 `evidence_version=0.4.1`。requirement_id、step_id、oracle_id、evidence_id 与依赖仍由原协议检查；每步运行结果绑定契约摘要、源码摘要、Observation ID、session_id 和尝试编号。跨步骤值只按声明的 ID/输出名解析，不使用任意表达式。

运行状态仅在当前 Pipeline 的局部数据结构中累积；冻结契约不可变。IPC 只传 JSON，Agent3 仅接收必要的状态副本及 DOM 元数据；没有跨 Agent 可变全局业务状态、共享 WebDriver 或 WebDriver 序列化。

## 4. 动态执行、目标与安全限制

执行链路：原始需求 → Agent1 TestDesign → Agent2 批准并冻结 → 当前会话 DOM Observation → Agent3 结构化操作与可信源码渲染 → Agent4 校验并执行 → 来源证据 → Agent5 验证 → 按预算补证/Agent6 恢复 → 下一步/终止 → HTML 报告。

本阶段新增 `targets.py`，内置 catalog/search、form/apply、download 三种本地起始页面，由 Pipeline 上下文负责服务启动与关闭。也可输入任意已启动的本地 HTTP(S) 服务 URL，不要求修改业务 Python。每次只允许起始 URL 的精确 origin（协议、主机和端口）；没有开放域名通配符、跨端口或自动扩大域名集合。

外部网站有独立入口和 `allow_live_site` 许可，执行前检查；额外补证和恢复仍需独立开关。目标配置返回 `start_url / allowed_origins / scope / navigation_policy`。新增 `allowed_actions` 在打开浏览器前检查整个契约，下载继续默认禁用、独立许可，不能依赖界面隐藏选项来实现安全。

保留前三阶段安全机制：CDP 发出前 origin 拦截；封闭动作与读取器；Worker 二次操作校验及源码逐字重渲染匹配；禁导入/反射/文件或进程任意操作；敏感 DOM 过滤；单步/总预算；过程异常关闭；网络/权限错误不降级绕过。子进程用于生命周期隔离，代码安全来自封闭操作，不是任意 Python 的 OS 沙箱。

## 5. Streamlit 与 CLI

保留原界面默认兼容流程和已有 `requirement=requirement` 传递。新增独立引擎选择：

- v0.3 兼容执行与 v0.4 动态执行。
- template 与 llm，含现有 provider/model 配置。
- 内置本地 Fixture、指定本地 URL、需授权外部 URL。
- 动态模板 JSON、允许动作、文件下载许可、补证/恢复预算和 live 额外许可。
- 只规划/生成与真实执行，互斥状态不会混用。

动态模板必须提交已编写 TestDesign，requirement 与提交原文相同；支持显式 `$TARGET_URL` 绑定临时 Fixture URL，其他语义不改写。没有把动态模板描述为自然语言推理。新增 `examples/download_design.json` 可无需 Key 验证完整原生下载流程。

结果页支持原始设计、审核记录、冻结契约、step_states、Oracle、分尝试代码、DOM Observation、定位依据、实际证据、补证和恢复、HTML 下载。审核拒绝/只规划结果不要求旧 inspector/evidence/case_id 等字段。失败的新运行会清掉之前结果，避免把上次 PASS 留作本次结果。

CLI 新增 `--engine / --fixture-site / --target-url / --execute-dynamic / --design-file / --allow-downloads / --allowed-actions`，旧参数和返回码规则兼容；动态规划完成返回成功退出码，但功能裁决仍 INCONCLUSIVE。

使用示例在 README 中可独立复现；远程环境无需依赖被 Git 忽略的 docs/AGENTS.md。

## 6. 跨场景和预先固定变体

验收使用实际 Chrome 和本机动态页面。多个 HTTP 服务在不同运行中启动不同 origin；搜索、表单两套应用的 DOM/交互结构不同，下载另成第三任务。

| 场景 | 完整链路与数据 | 预期/证据来源 |
| --- | --- | --- |
| catalog 基础 | 搜索“机器人”、选择分类、读取已应用分类、选第二项、导航并比较标题 | 实际 select 值经请求到结果页；结果/详情均实际 DOM 读取；冻结 Oracle 比较 |
| catalog layout | 相同语义任务，改变 ID/class、DOM 包装、布局和按钮可见文字 | 可访问名称保留；Agent3 只使用新 DOM 节点句柄，无新增业务选择器 |
| form layout | 姓名改为“李四”、城市改为“北京”，异步预览、返回并验证状态保留 | 表单真实写入、JS 更新与 sessionStorage 恢复；读取前后字段 |
| download | 下载实际 report.json，核对完成、大小/摘要和 JSON 基础有效性 | Chrome 实际下载，Agent5 读取运行目录文件校验 |
| missing | 在运行前删除搜索输入框 | 明确 ERROR、关闭会话，不能虚构 PASS |
| authored template | 相同受限下载功能，template 模式与 owned Fixture | 无模型、不做自然语言规划；验证用户可直接使用的入口 |

布局和缺失变体的代码在运行前固定；测试在第一次模型调用前保存页面 SHA-256、场景、需求、variant 和生成器源码摘要 `prepared_variant.json`。没有在失败后加验收站点专用 CSS/XPath 或修改模型提示来适配页面。实际业务 HTML/JS 仅在 Fixture 中；操作代码仅使用通用 Selenium 原语。测试的 Mock Agent1 响应与语义契约属于验收数据，不是生产执行器的网站模板。

泛化结论必须限定：这些变体保持可访问语义名称，因此验证的是对 ID、class、层次、布局、按钮可见文本和数据变化的适应，不证明任意控件名称/语义变化。Mock LLM 的规划响应是预编写的，不能当作真实模型理解新自然语言任务的证明。

Fixture 没有返回 PASS 常量。四类输出由浏览器执行和 Agent5 计算；负向 missing 验证 ERROR，前三阶段另有确认 FAIL/缺证共存及恢复失败的回归。

## 7. 报告与追踪

动态 `dynamic_report.html` 包含需求、目标范围、完整语义设计、审核、步骤状态、源码与摘要、定位审计、Observation、实际执行轨迹、证据、Oracle 和历史。内容转义，生成代码可展开查看并链接到原文件；旧 `html_report()` 路径未改写。

可追踪文件链：

```text
test_design_revisionN.json / test_design.json
  → review_decision.json → test_contract.json
  → observation_NNN.json
  → generation/S_step_attemptN/generation_S_step.json + dynamic_S_step.py
  → execution_attempts.json + dynamic_steps.json
  → dynamic_evidence.json → final_verdict.json
  → dynamic_result.json + dynamic_report.html
```

补证和恢复分别见 `dynamic_probes.json / dynamic_recovery.json`，旧尝试和源码保留。证据携带版本、ID、步骤、尝试、时间、来源、公开 URL、页面状态、session、契约/源码摘要及外部产物路径。下载文件二进制和完整 DOM 不复制进 Agent 间 JSON；报告中的 DOM 是有界公开摘要。

## 8. 测试分层与结果

1. Python 单元/协议：严格数据校验、Oracle、数值与文件验证、origin 和动作许可、预算、摘要及路径安全。
2. Mock LLM + Pipeline：Agent1 请求接收到原始需求；Agent2 通过/拒绝；Agent3 接收实时 Observation；六阶段记录，源码/证据关联。
3. 真实 Chrome + 本机 Fixture：本阶段完整跨场景链路、页面变体和负向场景，加上前三阶段搜索/表单、下载、只读补证、DOM 恢复、元素状态、网络阻断回归。
4. 真实 LLM + 浏览器：本次未执行，不宣称通过；没有主动访问 arXiv 或其他外部站点。

Streamlit 使用实际 `streamlit.testing.v1.AppTest` 执行脚本和控件事件：兼容初始界面、动态模板规划、拒绝结果、完成结果及源码/DOM 展示。它是 Streamlit 组件/运行逻辑测试，不是网页像素级视觉验收。CLI 用独立 Python 子进程实测动态模板规划及 JSON 输出，另检查帮助与原回归。

最终命令（本机 ChromeDriver 环境路径按实际安装设置）：

```powershell
python -B -m pytest -q -p no:cacheprovider
$env:WEBAGENT_REAL_CHROME='1'
$env:CHROMEDRIVER_PATH='C:\Users\35254\.cache\selenium\chromedriver\win64\156.0.8078.80\chromedriver.exe'
python -B -m pytest -q -p no:cacheprovider --basetemp artifacts/v04_stage4_acceptance_20261106/pytest --junitxml artifacts/v04_stage4_acceptance_20261106/pytest.xml
```

实际命令通过 Tee-Object 保存 pytest.log。不要复用已有验收 basetemp，pytest 会清理该目录。历史测试结果不能替代本次结果；本次最终数字见以下附录。

### 本次最终测试与证据（2026-11-06）

| 检查 | 本次实际结果 |
| --- | --- |
| 默认完整集合 | **225 passed、17 skipped，10.98 秒**；跳过项为需要显式启用的 Chrome 测试 |
| 启用真实 Chrome 的完整集合 | **242 passed，172.50 秒，0 failed / 0 errors / 0 skipped** |
| 本地真实 Chrome | **17 项**；含此前阶段恢复、补证、网络阻断回归及本阶段 6 项完整入口验收 |
| Python 语法解析 | **41 个文件通过**，包含工程、入口及测试 |
| 完整 Pipeline 源码对照审计 | **28 次执行源码摘要一致**；每个运行的执行记录均仅含一个浏览器 session_id，6 个完整入口运行均关闭会话 |
| 真实 LLM / 外部受测网站 | **未执行** |

负向浏览器测试符合预期地返回 ERROR，也属于 pytest 通过；242 passed 不表示 242 个应用任务全部得出 PASS。上述 28 次源码审计仅对应下表 6 个完整 Pipeline 运行。

环境：Python 3.12.3、Selenium 4.36.0、Chrome 156.0.8078.74、ChromeDriver 156.0.8078.80、已安装 Streamlit 1.32.0。未重新安装依赖。

完整集合：[JUnit](../artifacts/v04_stage4_acceptance_20261106/pytest.xml)、[原始日志](../artifacts/v04_stage4_acceptance_20261106/pytest.log)；默认集合：[JUnit](../artifacts/v04_stage4_acceptance_20261106/offline.xml)、[日志](../artifacts/v04_stage4_acceptance_20261106/offline.log)。

[机器可读审计摘要](../artifacts/v04_stage4_acceptance_20261106/verification_summary.json)保存 17 项 Chrome 目录索引、6 个完整运行的结果与会话、预先固定的变体摘要、41 个 Python 源文件摘要、Git HEAD 与工作区状态。[审计脚本](../artifacts/v04_stage4_acceptance_20261106/verify_archive.py)可复核现有归档，不启动浏览器或模型。归档保留原路径，避免移动后破坏证据的绝对路径引用。

| 完整入口验收 | 实际结论 | 源码、DOM、证据与执行记录所在的 HTML 报告 |
| --- | --- | --- |
| 搜索/筛选/第二项导航与跨页比较 | PASS | [搜索报告](../artifacts/v04_stage4_acceptance_20261106/pytest/test_real_chrome_full_six_agen0/20261106_100021_caa711/dynamic_report.html) |
| 搜索布局与 ID 变体 | PASS | [变体报告](../artifacts/v04_stage4_acceptance_20261106/pytest/test_real_chrome_full_six_agen1/20261106_100032_af78fa/dynamic_report.html) |
| 异步多步骤表单/布局与测试数据变体 | PASS | [表单报告](../artifacts/v04_stage4_acceptance_20261106/pytest/test_real_chrome_full_six_agen2/20261106_100042_853ea9/dynamic_report.html) |
| 文件下载与基础有效性 | PASS | [下载报告](../artifacts/v04_stage4_acceptance_20261106/pytest/test_real_chrome_full_six_agen3/20261106_100051_58c7c3/dynamic_report.html) |
| 缺失输入控件负向验收 | ERROR（预期） | [负向报告](../artifacts/v04_stage4_acceptance_20261106/pytest/test_real_chrome_missing_contr0/20261106_100100_50df84/dynamic_report.html) |
| 无模型 template + 自管本地 Fixture 下载 | PASS | [模板报告](../artifacts/v04_stage4_acceptance_20261106/pytest/test_real_chrome_builtin_templ0/20261106_100110_af6819/dynamic_report.html) |

最终 HEAD 仍为 `fe4a76dc8f41dc532aecc7734799c8e5fc74b04f`。工作区保留前三阶段及本阶段未提交修改，没有 commit、push 或 tag。`git diff --check` 通过；Git 另提示 LF/CRLF 自动转换及无法读取用户目录全局 ignore，这些提示不影响本次测试结果。docs/、AGENTS.md 和 artifacts/ 被 Git 忽略。

## 9. 本阶段代码变更与 Git 状态

| 文件 | 本阶段功能 |
| --- | --- |
| app_ui.py | 引擎/目标/模板/权限选项、动态结果兼容、逐步源码/审计/DOM/证据和报告展示 |
| run_demo.py | 动态入口参数、设计文件、动作/下载许可、兼容结果摘要与退出码 |
| web_agent/targets.py（新增） | 内置 Fixture 生命周期、受控显式 URL、精确 origin 元数据、显式模板 URL 占位绑定 |
| web_agent/pipeline.py | owned Fixture 调度、执行前目标/动作权限预检、动态报告和统一结果保存 |
| web_agent/dynamic_execution.py | immutable allowed_actions 策略、逐步二次动作许可 |
| web_agent/report.py | 独立动态 HTML 报告，支持仅规划与审核拒绝；旧报告保留 |
| web_agent/dynamic_fixture.py | 分类选择/结果状态、固定布局/ID/层次/按钮变体及缺失控件 |
| tests/test_v04_integration.py（新增） | 全 Pipeline Chrome 场景、变体与负向、目标/模板/报告、UI/CLI 集成 |
| examples/download_design.json（新增） | 可直接运行的 authored 语义设计，不含 Selenium 业务代码 |
| README.md | v0.4 安装/使用/接口/权限/能力分类与验证方式；保留旧演示指令 |
| 本报告及 artifacts | 本机最终验收材料，docs/artifacts 被忽略 |

前阶段 designer/reviewer/schema/verifier/recovery、Worker 和测试等未提交改动在最终工作区继续保留，不能把全部 Git diff 都算作本阶段重新开发。没有自动拆分六 Agent，没有修改旧 fixture_server.py 来伪装动态生成。没有 Git commit、push 或 tag。

## 10. 已知限制、待完善与最终验收口径

- 已实现并本地测试：六角色职责与协议、同会话动态操作、确定性功能验证、有限补证/恢复、UI/CLI/HTML 贯通、至少三个任务和两套不同结构应用。
- 部分实现：原生控件有限动作、单 origin、精确 aria-label 的缺失判断、有限文件类型基础校验；公开 DOM 过滤是有限规则，不是通用 PII 检测。
- 未验证：真实 LLM 的设计质量和独立审核效果、外部网站运行、Streamlit 的 Chrome 视觉/布局验收、真实线上故障发现；本轮不承诺通用网站自主测试。
- 未实现：跨 origin 认证、通用 iframe/Shadow DOM、复杂拖拽/编辑器、任意文件内容理解、浏览器崩溃后的任务迁移或无限自愈。
- 有副作用动作开始后不自动重放；只读补证不重建历史页面；硬超时会终止 Worker，迟到模型响应不能执行但线程可能继续落盘到独立尝试目录。
- 当前平台已安装 Streamlit 1.32.0（项目 requirements 声明 >=1.40），本次 AppTest 可用；未在全新虚拟环境重新安装依赖。真实 Chrome/Driver 版本由最终摘要保存。

达到目标的判断：**受限 v0.4 工程整合和本地浏览器验收目标已具备可核查证据；真实 LLM 自然语言泛化与线上可用性仍是明确待验收项。** 等待人工最终验收后再决定发布，不自动创建版本或继续开发。
