# WebAgent v0.4 第三阶段开发报告

日期：2026-11-04。范围：Agent5 动态 Oracle 验证、可追踪证据、主动只读补证、Agent6 有界执行恢复。未进入第四阶段，未提交或推送 Git。

## 1. 结论与基线

本阶段已将 Agent5/6 接入原有动态执行循环。语义契约不变，Agent4 采集带来源的证据，Agent5 确定性计算 Oracle；缺证时由 Pipeline 检查预算和副作用后调度 Agent4 补证，定位/交互错误则由 Agent6 给出恢复决定，并基于新的 DOM Observation 重新调用 Agent3。

- Git HEAD：`fe4a76dc8f41dc532aecc7734799c8e5fc74b04f`，交付时未改变。
- 开始时工作区已有前两阶段未提交修改，全部保留；已阅读本地 `AGENTS.md` 及任务指定的 Agent5/6、Evidence Plan、Probe Plan、Probe、Schema、Pipeline，并检查动态协议和执行 Worker。
- 本阶段开始时默认 pytest 实测：**139 passed，5 skipped，6.31 秒**。
- 最终默认离线 pytest：**213 passed，11 skipped，6.12 秒**。
- 最终启用真实 Chrome 的完整 pytest：**224 passed，0 failed，0 skipped，95.72 秒**。其中 11 项是本机 Fixture + 真实无头 Chrome，模型定位使用 Mock LLM。
- 对 39 个 Python 源码/测试/入口文件做 AST 语法检查，通过；`git diff --check` 通过，仅有 Windows LF/CRLF 提示。

材料保存在 [验收目录](C:/Users/35254/Desktop/WebAgent/artifacts/v04_stage3_acceptance_20261104)、[验证摘要](C:/Users/35254/Desktop/WebAgent/artifacts/v04_stage3_acceptance_20261104/verification_summary.json)、[JUnit 结果](C:/Users/35254/Desktop/WebAgent/artifacts/v04_stage3_acceptance_20261104/pytest.xml)。`docs/`、`artifacts/` 被 Git 忽略，报告和产物仅在本地，不代表远程已经包含。

## 2. Oracle 与版本策略

保留 v0.3 `QuerySpec/TestContract/Verdict` 和 Evidence Plan，动态契约继续使用 `protocol_version=0.4`，不改变既有字段形状。本阶段对动作、证据种类和关系枚举做向后兼容扩展；运行证据使用独立 `evidence_version=0.4.1`，避免把运行采集字段塞进语义设计。

Oracle 仍由 Agent2 审核后冻结，包含 `oracle_id / requirement_ids / after_step_id / relation / actual / expected / evidence_ids`。`actual` 只引用已声明 Evidence ID；`expected` 只允许原有 literal、test_data、evidence。跨页面比较通过两个 Evidence ID 连接，各自证据指向原步骤。步骤输入的跨步骤引用继续使用 `step_output + step_id + output`，依赖关系由 Schema 校验；没有任意表达式、eval 或模型提供的函数。

新增/支持的通用关系与读取类型：

| 断言 | relation | 证据读取 |
| --- | --- | --- |
| 字段相等/不等 | `equals / not_equals` | text、value、布尔等；相等比较要求类型一致 |
| 规范化文本相等 | `normalized_equals` | 合并空白、去边界空白、casefold |
| 文本包含 | `contains` | 严格的字符串包含 |
| 数值/数量比较 | `greater_than / less_than / greater_or_equal / less_or_equal` | count、number、可解析为数字的 value；拒绝 bool、NaN、Infinity、表达式 |
| 存在、可见、可用、选中 | `equals / boolean_equals` 等 | exists、visible、enabled、selected/boolean |
| URL/页面属性 | `equals / contains / not_equals` 等 | url、page_title |
| 跨步骤/页面字段 | 上述关系 | actual/expected 指向不同步骤的证据 |
| 文件完成与基础有效性 | `file_valid` | file，预期为冻结的布尔值 |

Oracle 的实现是应用维护的 `ORACLE_EVALUATORS` 注册表，新增关系需要同步 Schema 校验和回归测试，契约不能传入可执行处理器。Agent1 的协议指南同步列出新增枚举，但未改写其规划逻辑。

对“目标不存在”采用保守语义：只在审核契约明确 `target.role=exact_aria_label`、无序号、观察未截断时允许空节点；Worker 再遍历完整实时 DOM 确认该精确 aria-label 不存在。一般自然语言目标不会仅凭有界摘要里没看到就判定不存在。隐藏普通控件可出现在摘要中用于状态判断，隐藏文本不导出；密码、文件输入和私有子树仍排除。

## 3. 动态证据协议

每条运行证据严格校验以下字段：

```text
evidence_version, evidence_id, step_id, output, kind,
value, status, reason,
attempt, collection_attempt, collected_at, source,
url, page_state, session_id,
contract_digest, source_sha256, artifacts
```

- `attempt`：该步骤执行尝试；`collection_attempt`：原采集或补证尝试。
- `status=observed/missing/failed`。缺失/采集失败必须带原因，不能拿空字符串、False、0 假装缺证；这些都是有效观察值。
- `url` 去除查询与片段；`page_state` 关联 Observation ID；时间使用带时区 UTC。
- `source` 为 selenium/probe/download；契约和实际编译源码摘要绑定语义与执行实现。
- `artifacts` 仅存相对路径：有界公开 DOM 摘要 `worker_dom_*.json`，下载时另有文件路径。JSON 不内嵌截图、完整 DOM 或文件二进制。本阶段不自动新增截图，避免无授权采集敏感页面内容。
- 产物路径必须留在当前运行目录，禁止绝对路径、越界路径和符号链接解析后的目录逃逸。

校验结果保存 `oracle_id / step_id / evidence_ids / evidence_attempts / relation / actual / expected / passed / reason`，实现：

```text
Step ID → Evidence ID + execution attempt → Oracle ID → Verification Result
```

来源摘要用于可信本地流程的关联与误改检测，不是密码学签名，不能把外部自行构造的 JSON 当作已认证证据。第二阶段旧的无来源短证据不能直接满足第三阶段 PASS，需要重新采集。本阶段未修改既有冻结契约摘要算法。

文件证据只在契约明确 `download` 动作且 `ExecutionPolicy(allow_downloads=True)` 时采集。Chrome 下载权限只在该动作内临时开启，完成后立即恢复 deny。限定同 origin 的 anchor，每次执行使用独立运行子目录；读取单个已完成、非临时后缀文件，最多 10 MiB，记录相对路径、大小、SHA-256 和完成标识。Agent5 再核对实际文件大小和摘要，检查非空；PDF 检查头与 EOF，JSON 检查可解析性。其他类型只做完成、完整性、非空基础检查，不宣称验证任意文件业务内容；未执行下载文件。

## 4. Agent5/6 接口与职责

Agent5 纯验证/规划接口，不操作浏览器，不改期望值：

```python
verdict = Agent5EvidenceVerifier().verify_dynamic(
    contract, evidence_by_id,
    execution_status="COMPLETE", error=error, run_dir=run_dir,
)
request = Agent5EvidenceVerifier().plan_dynamic_probe(
    contract, evidence_by_id, verdict, step_id=current_step_id,
)
```

`verify()` 保留旧入口，同时能够按动态契约对象分派。文件验证要传入可信 run_dir。动态证据缺失、来源不匹配、文件变更等均无法支持 PASS；采集失败不会自动被判为业务失败。

Agent6 纯恢复判断接口：

```python
decision = Agent6RecoveryController().plan_dynamic_repair(
    contract, structured_error, fresh_observation,
    previous_repairs=count, max_repairs=budget,
    live_recovery_allowed=False, verdict=current_verdict,
)
```

输出 `type / allowed / strategy / contract_digest / step_id / category / reason / attempt / observation_id`。仅允许 `regenerate_from_fresh_dom` 或停止。分类区分 application_assertion、locator、interaction、wait、evidence_missing、environment、permission、invalid_code。应用断言 FAIL 为终止结论；缺证走 Agent5；环境/权限/代码非法不作浏览器方法恢复。

Worker 将 Selenium 定位、交互、等待异常返回为结构化结果，记录 `action_started`、步骤、尝试、原始异常类和 API 轨迹。生成代码非法、权限越界、网络防护失败仍中止 Worker。各 Selenium 等待共享尝试截止时间，保留部分 RPC 时间返回结构化超时；真正阻塞仍由父进程硬截止和进程树终止兜底。

## 5. 补证与恢复调用过程

### 主动补证

1. 每个步骤完成后，Pipeline 合并原采集证据并调用 Agent5。只要已确认 FAIL 就停止后续动作，不再补证或恢复。
2. Agent5 针对当前检查点缺失且参与 Oracle 的证据生成 `dynamic_read` 请求，引用原契约摘要、step_id、evidence_ids、oracle_ids 和 read_only 标记。
3. Pipeline 检查预算、原步骤是否 observe/wait、live 补证额外许可，以及 Oracle/步骤/证据的真实关联。
4. Agent4 在同一浏览器会话重新观察；Agent3 为同一语义步骤生成新读取实现，Pipeline 保存独立源码与审计；Agent4 校验后执行只读片段。
5. `merge_dynamic()` 只填补缺失、失败或无效记录，不覆盖任何有效 observed 记录，包括 False、0 或与新采集冲突的已观察值。随后再次调用 Agent5。

当前实现只补采原步骤刚完成时的只读检查点，不自动返回历史页面，不重放 click、submit、navigate、download。数据已变化的历史检查点宁可 INCONCLUSIVE。全局默认最多 1 次补证，可设 0～3；live 模式还需要 allow_live_site 和 allow_live_probes。

### 执行恢复

1. 执行发生结构化 locator/interaction/wait 错误，Pipeline 检查剩余预算和许可，在同一会话取得新 DOM。
2. Agent6 判断是否仅通过新的定位/执行方式重试原步骤，保证冻结契约摘要不变。
3. Pipeline 独立复查决定、权限、副作用及预算；Worker 对真实失败历史和重试也做二次检查。
4. 新调用 Agent3，生成文件存入 `generation/S_<步骤>_attemptN/`，保留旧源码；执行结果按尝试追加 `execution_attempts.json`，不覆盖失败记录。
5. 成功后继续；修复仍失败或预算耗尽则 ERROR。默认全局最多 1 次恢复，可设 0～3，不能无限循环。

**一旦动作已经开始，除 observe/wait 外均不自动重放**，包括 fill、select、check（它们也可能触发应用事件），以及点击、提交、下载。动作发出后等待异常不能被恢复成再次提交。真实浏览器验证的恢复发生在过期元素定位阶段，动作尚未发出。

## 6. 状态语义与旧版差异

| 状态 | 本阶段规则 |
| --- | --- |
| PASS | 执行完成，所有必要 Oracle 均由通过来源校验的证据支持且通过 |
| FAIL | 至少一个 Oracle 有可信证据确认失败，即使另有缺证或执行异常也保留该确认失败 |
| ERROR | 未确认功能失败，且执行错误阻止正常验证 |
| INCONCLUSIVE | 尚未确认失败，未被执行错误阻断，但必要证据不足/未执行/没有必要 Oracle |

第二阶段动态执行成功始终 INCONCLUSIVE；本阶段在证据充分、Oracle 全通过时才允许动态 PASS。`execution_status=COMPLETE` 自身依然不代表 PASS。

明确授权的旧版行为修正：原 verifier 在同次完整执行中已有 Oracle 失败且另一个缺证时先返回 INCONCLUSIVE；现将确认 FAIL 放在缺证判断之前。独立回归 `test_legacy_confirmed_fail_with_missing_evidence_regression` 验证 `[False, None] → FAIL`，并更新原测试中对此旧行为的断言。旧执行状态为 ERROR 时仍保留其既有早期 ERROR 路径。

Selenium 异常只表示执行工具错误，不宣称发现应用 Bug；可靠 FAIL 不会通过反复重生成代码被消除。

## 7. 代表性验证记录

以下均为本次真实无头 Chrome + 本地 Fixture，模型使用 Mock；完整记录见各子目录 `dynamic_execution.json`、`dynamic_evidence.json`、`execution_attempts.json`、`dynamic_probes.json`、`dynamic_recovery.json`。

| 场景 | 实际行为与结果 |
| --- | --- |
| search | 搜索、选择第二项、跨页面读取；`E_result` 与 `E_detail` 均为“机器人乙”，O_title PASS |
| form | 姓名/城市、异步预览、返回；预览与返回字段均为“张三”，两个 Oracle PASS |
| download | 本机真实 HTTP 下载 report.json；实际文件 `{ "fixture": true, "count": 2 }`，大小/摘要/JSON 可解析性检查 PASS |
| probe | 首次对无 value 属性的段落读取失败；同语义目标用新 DOM 定位 output，只读补证 1 次，证据 source=probe、collection_attempt=2，最终 PASS |
| recovery | Fixture 在规划与执行之间替换搜索框；首次 StaleElementReferenceException、action_started=false；同 session 用新 Observation 再生成，修复 1 次，最终 PASS |
| absence/hidden/disabled | 精确标签不存在、隐藏按钮不可见、禁用按钮不可用，实际布尔读取支持对应 Oracle PASS |
| security_redirect/image/worker | 越界重定向、图片和 Worker 请求被拦截，工具结果 ERROR；测试断言阻断成功，因此 pytest 用例通过 |

验收归档再次核对每个实际执行尝试的源码 SHA-256、补证片段摘要和下载文件摘要。源码摘要按 UTF-8、文本读取后 LF 换行规范化计算；原始 Windows CRLF 文件字节摘要可能不同。

模拟/单元测试另外验证：确认 FAIL 与缺证共存、补证失败、错误修复后仍失败、预算耗尽、额外 live 授权缺失、伪造 Agent6 许可被 Pipeline 拒绝、旧补证/恢复兼容。上述负向逻辑测试不冒充真实网站验收；真实浏览器没有访问线上业务服务。

## 8. 文件清单与实现功能

| 文件 | 本阶段修改/新增功能 |
| --- | --- |
| 新增 `web_agent/dynamic_verification.py` | v0.4.1 证据构造/校验、路径限制、Oracle 注册表与数值解析、文件验证、补证协议、非覆盖合并 |
| `web_agent/dynamic_schema.py` | 新增 download 动作、证据种类、关系枚举及 file_valid 类型校验；模型协议指南同步 |
| `web_agent/schema.py` | 注明动态运行证据独立版本，保持旧数据类 |
| `web_agent/agents/verifier.py` | 动态 verify/probe 接口；确认 FAIL 优先于部分缺证 |
| `web_agent/agents/recovery.py` | 动态结构化 Recovery Decision、分类、预算、授权、副作用限制 |
| `web_agent/agents/generator.py` | 新读取类型、下载片段、明确缺失元素语义、状态读取的模板模式；不加入网站业务逻辑 |
| `web_agent/dynamic_execution.py` | 新策略字段、错误分类、封闭读取器校验、只读状态与下载可信源码渲染 |
| `web_agent/browser_worker.py` | 来源证据、文件采集、只读补证、结构化错误、动作开始标记、重试二次校验、尝试截止时间 |
| `web_agent/browser_session.py` | probe IPC、分类错误传播、剩余步骤超时传给 Worker |
| `web_agent/dom_inspector.py` | 隐藏普通控件状态摘要、正确截断标记；隐藏文本仍过滤 |
| `web_agent/pipeline.py` | 逐检查点验证、补证与恢复编排、额外授权检查、终止 FAIL、尝试归档与最终结果 |
| `web_agent/dynamic_fixture.py` | 下载页面、检查控件、可触发/确认的 DOM 变化，用于确定性真实浏览器测试 |
| 新增 `tests/test_v04_verification.py` | 74 项离线测试、6 项新增真实 Chrome 测试 |
| `tests/test_v04_execution.py` | 第二阶段浏览器测试现由 Agent5 验证为 PASS；适配分尝试源码路径与六阶段记录；保持其余安全/预算测试 |
| `tests/test_v02_agent_upgrade.py` | 更新授权修正的 FAIL+缺证语义断言 |
| 本报告与 artifacts 归档 | 实际验证记录、完整测试结果、源码/证据摘要和能力边界 |

原 `evidence_plan.py / probe_plan.py / probe.py / selenium_runtime.py / web_agent.generator` 兼容实现本阶段未改；v0.3 固定字段白名单仍约束旧路径，动态路径按冻结 Evidence ID 采集，不使用该白名单。

## 9. 测试命令与未验证项

```powershell
# 默认离线：213 passed, 11 skipped in 6.12s
python -B -m pytest -q -p no:cacheprovider

# 最终完整：224 passed in 95.72s
$env:WEBAGENT_REAL_CHROME='1'
$env:CHROMEDRIVER_PATH='C:\Users\35254\.cache\selenium\chromedriver\win64\156.0.8078.80\chromedriver.exe'
python -B -m pytest -q -p no:cacheprovider --basetemp artifacts/v04_stage3_acceptance_20261104/pytest --junitxml artifacts/v04_stage3_acceptance_20261104/pytest.xml
```

实际完整命令另通过 Tee-Object 保存 pytest.log。复跑请换新的 basetemp，避免 pytest 清理已有验收证据。环境沿用 Python 3.12.3、Selenium 4.36.0、Chrome 156.0.8078.74、ChromeDriver 156.0.8078.80。

未验证/边界：

- 没有真实 LLM、真实 arXiv/线上网站、复杂跨域认证或 Streamlit 交互验收；本次没有重新实跑旧版真实浏览器演示，旧版离线回归已通过。
- 下载只支持当前契约明确声明的同源链接，默认禁用；没有任意文件内容理解、病毒检测或执行下载内容。下载大小校验发生在落盘后，不是网络层磁盘配额。
- 恢复只调整未开始动作的定位/交互方法，或重试只读动作；不支持浏览器崩溃后的状态迁移，不自动重新登录、不重新提交表单。
- 补证不重建历史页面状态，不自动导航补历史字段；只读页面也可能自行刷新或运行网站脚本，因此证据时间/页面状态保留供审查。
- 有界 DOM 摘要不等于完整视觉理解；精确缺失语义仅支持 aria-label，iframe/Shadow DOM/复杂控件的原有限制继续存在。隐私过滤为有限规则，不是通用 PII 检测。
- 旧式第二阶段证据缺少第三阶段来源字段，不直接升级为可信记录；需要重新执行采集。
- Worker 子进程是生命周期隔离，封闭操作与源码二次校验是代码执行边界，不声称为任意 Python 或恶意浏览器内容提供 OS 沙箱。
- 模型调用超时后的线程不能被 Python 强杀；它不持有浏览器，迟到结果不会执行，但可能迟到写入其独立尝试目录。

当前交付仅到第三阶段，等待人工验收。
