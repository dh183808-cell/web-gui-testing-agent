# WebAgent v0.4 阶段 1 开发报告

日期：2026-11-04  
阶段：开发阶段 1/4——动态测试语义协议、Agent1 测试设计与 Agent2 需求审核  
开发前 Git HEAD：`fe4a76dc8f41dc532aecc7734799c8e5fc74b04f`

## 1. 本阶段完成情况

本阶段在现有六 Agent 模块化结构上，增加了跨网站、多步骤任务的语义设计与审核入口，没有重新拆分 Agent，也没有建立另一套 Agent 框架。

主要完成以下工作：

1. 新增独立的 v0.4 动态语义协议，区分候选设计与审核后契约。
2. 扩展 Agent1，使其能够通过可注入的 JSON 模型生成多步骤语义设计，并接收审核反馈修订。
3. 扩展 Agent2，结合确定性检查和独立 LLM 语义审核，识别要求偏差、验证遗漏及未确认信息。
4. 将测试引擎选择与模型规划方式分离。
5. 在 Pipeline 中接入最多两轮的动态设计与审核流程。
6. 增加 49 项自动化测试，保留 v0.3 原有流程。

**完成范围是设计、审核和契约冻结。动态任务没有进入浏览器执行，审核通过也不代表功能测试通过。**

## 2. 新增和修改的文件

本次产品代码与测试共涉及 6 个文件：新增 2 个、修改 4 个。本文是后续按要求新增的本地开发报告，不计入这 6 个文件。

| 文件 | 变更类型 | 变更内容及实现功能 |
| --- | --- | --- |
| `web_agent/dynamic_schema.py` | 新增 | 定义 v0.4 语义协议、严格解析、关联校验、依赖排序、修订 ID 检查和不可变契约。 |
| `web_agent/schema.py` | 修改 | 导出 `TestDesign`、`DynamicTestContract`，新增 `parse_contract()`，支持新旧契约共存。 |
| `web_agent/agents/designer.py` | 修改 | 在原 Agent1 中增加动态设计分支、跨网站规划提示、需求 ID 绑定和审核反馈修订接口。 |
| `web_agent/agents/reviewer.py` | 修改 | 在原 Agent2 中增加动态协议检查、需求覆盖检查、独立语义审核和通过后的契约冻结。 |
| `web_agent/pipeline.py` | 修改 | 增加独立的引擎选择和动态规划分支，管理审核次数、反馈和规划产物。 |
| `tests/test_v04_planning.py` | 新增 | 增加两个业务场景及协议、审核、兼容性、阶段边界等 49 项参数化测试。 |

Agent3～Agent6、Selenium Runtime、LLM Adapter、CLI、Streamlit 页面和原有测试文件没有修改。

## 3. 动态语义协议

### 3.1 新旧协议分离与版本识别

旧版 `QuerySpec`、`TestContract`、`Verdict` 和 `JSONModel` 保留。没有把旧版查询参数对象扩展为包含大量可空字段的通用任务对象。

v0.4 新增两个主要对象：

- `TestDesign`：Agent1 生成的候选语义设计，尚未获得审核批准。
- `DynamicTestContract`：Agent2 审核通过后冻结的语义契约。

新协议使用 `protocol_version="0.4"`，并通过 `document_type` 区分 `test_design` 和 `test_contract`。`parse_contract()` 将没有版本标识的原有契约按 v0.3 解析，也接受显式标识的 v0.3 契约；未知版本被拒绝。旧版契约的原有输出字段没有改变。

### 3.2 主要数据对象

| 对象或字段 | 表达内容 |
| --- | --- |
| `requirement`、`target_url`、`test_goal` | 原始需求、目标网站和测试目标。 |
| `Requirement` | 原子需求、原文引用、动作或验证分类及稳定 `requirement_id`。 |
| `SemanticTarget` | 操作目标的语义描述、角色，以及有序目标的一起始位置编号。 |
| `SemanticStep` | `step_id`、需求关联、动作、语义目标、依赖、前置条件、输入及输出声明。 |
| `Precondition` | 前置条件描述、用户已提供或未知状态，以及对应原文引用。 |
| `TestDatum` | 用户指定的标量测试数据、`data_id` 和原文来源。 |
| `ValueReference` | 字面值、测试数据、前序步骤输出或证据引用，避免把模型文本当成执行代码。 |
| `EvidenceRequirement` | `evidence_id`、采集步骤、对应输出、证据含义及类型。 |
| `SemanticOracle` | `oracle_id`、需求关联、判定时机、比较关系、比较双方和所需证据。 |
| `assumptions`、`missing_information`、`unsupported_requests` | 未确认假设、缺失信息和不支持的要求。 |

步骤动作词汇包括 `navigate/fill/click/select/check/submit/observe/wait`。这些词汇在本阶段用于表达语义设计，没有新增对应的动态执行器。

### 3.3 严格校验与稳定标识

协议解析不进行宽松类型转换，并检查：

- 必需字段、未知字段、版本、动作类型和目标 URL 格式。
- ID 格式、重复 ID、未知需求和前置条件引用。
- 循环依赖、悬空依赖、未声明输出，以及引用未来或非依赖步骤数据的情况。
- 测试数据的原文来源，阻止没有来源的字面量直接作为步骤输入。
- Oracle 关联的证据是否存在、是否在判定时可用，以及证据列表与比较双方是否一致。
- 数值比较的基本类型要求；截图不能直接作为标量比较对象。

`requirement_id` 由原始需求与准确引用片段计算。步骤、Oracle 和证据使用语义 ID，修订时会检查未改变的实体是否保留原 ID。`ordered_steps()` 提供稳定的拓扑排序，后续模块不需要把 JSON 数组顺序直接当作执行顺序。

### 3.4 审核后冻结

审核通过后，设计被复制为包含冻结数据类、元组和标量的不可变快照，并记录修订号、审核时间和设计摘要。

`assert_integrity()` 和反序列化入口会检查摘要，检测序列化语义内容被修改的情况。摘要用于完整性检查，不是身份认证或防恶意重新计算摘要的签名机制。

## 4. Agent1：动态测试设计

原有 `design()` 接口保留默认 legacy 行为，并新增动态分支及 `design_dynamic()`。

动态输入包括原始需求、目标 URL、规划方式、可注入的 `JSONModel`、审核反馈和上一版设计；输出为经过协议校验的 `TestDesign`。

新增功能包括：

- 通过 LLM 将不同网站需求拆解为语义步骤，而不是固定生成 arXiv 查询参数。
- 建立动作依赖、输入数据和前序输出引用。
- 记录用户指定的数据及其来源。
- 标注未知前置条件、假设、缺失信息和不支持要求。
- 把模型提供的局部需求标识绑定为应用计算的稳定需求 ID。
- 接收审核反馈和上一版设计，检查原始需求、目标网站及未改变实体的 ID。

Agent1 不检查 DOM，不生成 CSS、XPath 或 Selenium 代码，也不启动浏览器。无效模型输出会被拒绝，不会通过回退到旧模板掩盖失败。

## 5. Agent2：动态需求审核

原有审核流程保留；新增动态分支及 `review_dynamic()`。审核时单独接收原始需求与目标网站，避免仅依赖 Agent1 自报的需求分解。

确定性检查覆盖需求与步骤、Oracle 的关联，验证要求是否有对应 Oracle，常见中英文序号是否与语义目标位置一致，以及 Oracle 是否比较同一个可观测值。存在未确认假设、缺失信息、不支持要求或未知前置条件时，不允许批准。

LLM 审核通过同一 `JSONModel.complete_json()` 接口独立调用，检查原始需求的整体覆盖、目标和数据是否正确、是否遗漏操作或验证、是否增加未经要求的操作及预期值。审核 JSON 也有严格字段和类型约束。

审核输出为：

- 通过：冻结的 `DynamicTestContract` 和审核决定。
- 退回：`None` 和包含具体问题的审核决定。
- 无效协议或审核 JSON：抛出 `ValueError`。

确定性问题不能被 LLM 返回的批准覆盖。任意自然语言的完整覆盖仍依赖语义审核，不能把这些规则视为对模型理解正确性的形式化证明。

## 6. 模式分离与 Pipeline 衔接

| 维度 | 取值 | 含义 |
| --- | --- | --- |
| 测试引擎 `engine` | `legacy` / `dynamic` | 旧版兼容流程或 v0.4 动态规划流程。 |
| 规划方式 `mode` | `template` / `llm` | 显式模板设计或可注入 JSON 模型。 |

`legacy` 保持原有模板及 LLM 行为。动态 `llm` 模式执行语义规划；动态 `template` 模式要求调用者提供明确的 v0.4 `template_design`，不会自动理解任意需求，也不会回退到 arXiv。

Pipeline 新增 `_plan_dynamic()`，依次调用 Agent1 和 Agent2。LLM 模式最多审核两轮，首次被退回时把问题及上一版设计交回 Agent1。显式模板被拒绝后直接返回，避免对无法自行修订的同一模板重复审核。

审核通过返回 `planning_status="PLANNED"`，未通过返回 `REVIEW_REJECTED`；两种情况都保持 `execution_status="NOT_RUN"`，功能裁决为 `INCONCLUSIVE`，不会返回 `PASS`。

动态分支不调用 Agent3～Agent6，不生成 Selenium 脚本，不启动 Fixture 或 DOM 检查，不访问目标网站。CLI 和 Streamlit 仍使用原有 legacy 流程；本阶段通过 Python 接口衔接动态规划。

## 7. 两个规划场景

以下样例均使用 Mock LLM 实际运行 Agent1、Agent2 和契约冻结流程。示例域名没有被访问，不能据此声称真实网站或真实模型已经通过验证。

| 场景 | 搜索导航 | 多步骤表单 |
| --- | --- | --- |
| 目标网站 | `https://catalog.example/search` | `https://enroll.example/apply` |
| 用户目标 | 搜索“机器人”，打开第二项，确认详情标题与结果标题一致。 | 填姓名“张三”、城市“杭州”，进入预览并返回，检查姓名正确且保留。 |
| 步骤 | 打开页面 → 输入关键词 → 提交 → 读取第二项标题 → 打开第二项 → 读取详情标题。 | 打开页面 → 填姓名 → 选城市 → 下一步 → 读取预览姓名 → 返回 → 读取姓名值。 |
| Oracle | 详情标题与第二项结果标题相等。 | 预览姓名、返回后的姓名分别与用户数据“张三”相等。 |
| 数量 | 6 步、1 个 Oracle。 | 7 步、2 个 Oracle。 |
| 最终状态 | `PLANNED / NOT_RUN / INCONCLUSIVE` | `PLANNED / NOT_RUN / INCONCLUSIVE` |

完整规划样例保存在 `artifacts/v04_stage1_acceptance/search_navigation.json` 与 `artifacts/v04_stage1_acceptance/multistep_form.json`。

## 8. 自动化测试与实际验证记录

本节引用本阶段开发时已经实际执行的结果，以及 `artifacts/v04_stage1_acceptance/verification.json` 中保存的验收记录。本次编写报告没有重新运行测试。

| 检查 | 实际结果 |
| --- | --- |
| 开发前工作区 | 干净。 |
| 开发前 pytest 基线 | 45 通过、0 失败。 |
| 开发后完整 pytest | 94 通过、0 失败，耗时 5.08 秒。 |
| 新增测试 | 49 项参数化用例。 |
| Python 语法检查 | 31 个文件通过。 |
| 模块导入检查 | 20 个项目模块通过。 |
| 类型注解解析 | 新协议、Schema、Agent1 和 Agent2 检查通过。 |
| Git 差异空白检查 | `git diff --check` 通过。 |

完整测试命令：

```powershell
python -B -m pytest -q -p no:cacheprovider
```

新增测试覆盖两个业务场景、错误选择第一项而不是第二项、遗漏验证、未知前置条件、无来源测试数据、无效 LLM JSON、依赖和数据引用、Oracle 与证据关联、契约冻结和内容修改检查、ID 稳定性、新旧协议共存、审核次数限制及动态流程不调用旧执行模块。

原有 45 项测试没有修改且全部通过。测试使用 Mock LLM，不需要真实 API Key。

## 9. 兼容性、限制与后续协议接口

v0.3 的参数对象、契约字段、四类裁决、主要 CLI 和 Streamlit 用法保持兼容。本阶段没有修改原有 arXiv 执行、有限补证和定位器恢复算法。

仍未验证或尚未实现的部分：

- 真实 LLM 对任意自然语言任务的规划与审核质量。
- 真实网站访问及动态 Chrome 端到端执行。
- 动态步骤对应的 DOM 定位、脚本生成、执行与运行时证据采集。
- 动态任务的 CLI／Streamlit 交互入口。
- 任意业务的完整语义校验；当前序号等确定性规则仅覆盖明确实现的模式。

阶段 2 可以使用以下接口，但本阶段没有开始阶段 2 开发：

1. `parse_contract(document)`：根据版本得到旧版或动态契约。
2. `DynamicTestContract.assert_integrity()`：检查冻结语义内容的完整性。
3. `contract.design.ordered_steps()`：得到满足依赖的语义步骤顺序。
4. 步骤的 `target/inputs/outputs/precondition_ids`：作为后续目标解析和数据绑定的输入。
5. `design.oracles` 和 `design.evidence_requirements`：作为后续验证与取证需求。

后续模块应消费已冻结的语义目标和 Oracle，不应擅自改变测试对象、步骤目标或预期关系。新增执行能力仍需要独立开发与验收。

## 10. Git 与文件保护

本阶段未自动提交或推送 Git，未读取或修改真实 API 配置。验收产物保存在被忽略的 `artifacts/` 中。本文新增于被忽略的 `docs/`，没有覆盖已有的 `docs/VERIFICATION_STATUS.md`，也没有把本地资料加入 Git。
