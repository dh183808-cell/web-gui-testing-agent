"""Streamlit UI for the v0.3 compatible and v0.4 dynamic six-agent demos.

Run: streamlit run app_ui.py
"""
from pathlib import Path
import json

import streamlit as st
from web_agent.pipeline import DEFAULT_REQUIREMENT, run_pipeline
from web_agent.dynamic_execution import ExecutionPolicy
from web_agent.targets import is_loopback

st.set_page_config(page_title="Web GUI Testing Agent · v0.4", page_icon="🧪", layout="wide")
st.markdown("""
<style>
.block-container{max-width:1240px;padding-top:2.4rem}
.hero{background:linear-gradient(115deg,#132a53,#255d88);color:white;padding:30px 35px;border-radius:16px;margin-bottom:18px}
.hero h1{color:white;font-size:34px;letter-spacing:-.4px;margin:0 0 5px}
.hero p{color:#cee3f9;margin:0}
.role{border:1px solid #dfe8f4;background:#f9fbff;border-radius:12px;padding:15px 12px;min-height:107px}
.role strong{display:block;color:#183c68;margin-bottom:5px}
.role small{color:#536e8d;font-size:12px}
</style>""", unsafe_allow_html=True)
st.markdown('<div class="hero"><h1>Web GUI Testing Agent <span style="font-size:17px">v0.4</span></h1>'
            '<p>自然语言需求 · 动态六角色协作 · 持久 Selenium 会话 · 证据驱动验证</p></div>',
            unsafe_allow_html=True)
roles = [
    ("Agent1", "测试用例设计"), ("Agent2", "用例审核 / Evidence Plan"),
    ("Agent3", "DOM + LLM 定位方案"), ("Agent4", "浏览器执行 / 取证"),
    ("Agent5", "跨页面验证"), ("Agent6", "报告 / 恢复决策"),
]
cols = st.columns(6)
for col, (name, desc) in zip(cols, roles):
    col.markdown(f'<div class="role"><strong>{name}</strong><small>{desc}</small></div>',
                 unsafe_allow_html=True)
st.write("")
left, right = st.columns([1.2, 0.8], gap="large")
with left:
    st.subheader("测试任务")
    requirement = st.text_area("自然语言测试要求", value=DEFAULT_REQUIREMENT, height=112)
    engine_label = st.radio("测试引擎", ["v0.4 动态流程", "v0.3 兼容流程"], index=1, horizontal=True)
    engine = "dynamic" if engine_label.startswith("v0.4") else "legacy"
    target_label = st.selectbox("被测环境", ["本地搜索 Fixture", "本地表单 Fixture", "本地下载 Fixture", "指定本地起始 URL", "外部网站（需明确授权）"] if engine == "dynamic" else ["本地 arXiv 风格仿真页（正常）", "本地 arXiv 风格仿真页（受控缺陷）", "真实 arxiv.org（需符合站点规则）"])
    if engine == "dynamic":
        fixture_site = {"本地搜索 Fixture":"catalog", "本地表单 Fixture":"form", "本地下载 Fixture":"download"}.get(target_label)
        target_url = st.text_input("起始 URL", value="", placeholder="http://127.0.0.1:8000/") if fixture_site is None else None
        st.caption("浏览器只允许起始 URL 的同源地址（含端口），其他域名和端口不会自动开放。")
        target = "dynamic"
    else:
        fixture_site = None
        target_url = None
        target = ["fixture", "fixture-bug", "arxiv"][
            ["本地 arXiv 风格仿真页（正常）", "本地 arXiv 风格仿真页（受控缺陷）", "真实 arxiv.org（需符合站点规则）"].index(target_label)]
with right:
    st.subheader("执行配置")
    mode_label = st.radio("Agent1/2/3 运行模式", ["模板演示（无需 API Key）", "真实 LLM（DeepSeek / OpenRouter / OpenAI）"], index=0)
    mode = "llm" if mode_label.startswith("真实") else "template"
    template_design = None
    template_text = ""
    if engine == "dynamic" and mode == "template":
        st.caption("动态模板模式需要已编写的 TestDesign JSON；不会从任意自然语言自动规划。target_url 可写 $TARGET_URL，requirement 必须与左侧原文一致。")
        template_text = st.text_area("动态 TestDesign JSON", value="", height=160)
    provider = None
    model = None
    if mode == "llm":
        provider_label = st.selectbox("模型服务商", ["DeepSeek", "OpenRouter", "OpenAI"], index=0)
        provider = {"DeepSeek": "deepseek", "OpenRouter": "openrouter", "OpenAI": "openai"}[provider_label]
        model_hint = {"deepseek": "deepseek-flash（默认）", "openrouter": "例如 provider/model（以 OpenRouter 当前列表为准）", "openai": "gpt-4.1-mini（默认）"}[provider]
        model = st.text_input("模型名称（留空使用 .env 或服务商默认值）", value="",
                              placeholder=model_hint) or None
        st.caption("密钥由 .env 或环境变量读取：DEEPSEEK_API_KEY / OPENROUTER_API_KEY / OPENAI_API_KEY。")
    headless = st.checkbox("无界面浏览器（后台截图）", value=True)
    demo_delay = st.slider("可视化演示停顿（秒）", min_value=0.0, max_value=2.0, value=0.0, step=0.25,
                           disabled=headless)
    only_generate = st.checkbox("只生成脚本，不运行浏览器", value=False)
    recovery_on = st.checkbox("Agent6：允许最多一次定位器局部恢复", value=True)
    probe_on = st.checkbox("Agent5：允许最多一次定向补证", value=True)
    execute_dynamic = engine == "dynamic" and not only_generate
    allow_downloads = engine == "dynamic" and st.checkbox("允许契约声明的同源文件下载", value=False)
    actions = None
    if engine == "dynamic":
        actions = st.multiselect("允许执行的动作", list(ExecutionPolicy().allowed_actions),
            default=list(ExecutionPolicy().allowed_actions))
    injection = "无"
    if engine == "legacy" and target != "arxiv" and not only_generate:
        injection = st.selectbox("演示故障注入（仅本地仿真页）",
            ["无", "模拟缺失详情标题证据（Agent5）", "模拟关键词定位器失效（Agent6）"])
    enable_live_probes = enable_live_recovery = False
    external = target == "arxiv" or engine == "dynamic" and target_label.startswith("外部")
    if external:
        st.caption("真实站点默认不会自动重复访问或修复重跑，需要额外单独确认。")
        enable_live_probes = st.checkbox("单独允许一次真实站点定向补证", value=False)
        enable_live_recovery = st.checkbox("单独允许一次真实站点定位故障恢复重跑", value=False)
    allow_live = st.checkbox("我已确认目标站点允许此次低频测试，不做批量抓取或绕过访问限制", value=False) if external else False
    st.caption("动态模式支持有限原生控件和证据类型，实际能力以审核契约及运行结果为准。" if engine == "dynamic" else "兼容模式保留 arXiv 参数化搜索流程。")
run = st.button("▶ 开始六 Agent 测试", type="primary", use_container_width=True)
if run:
    if external and not allow_live:
        st.error("请先确认目标网站自动化访问符合适用规则。")
    else:
        with st.spinner("正在构建测试契约、生成 Selenium 脚本并处理执行证据…"):
            try:
                st.session_state.pop("result", None)
                if engine == "dynamic" and target_label.startswith("指定本地") and not is_loopback(target_url):
                    raise ValueError("本地 URL 只允许 loopback；外部网站请选择授权入口")
                if engine == "dynamic" and mode == "template":
                    template_design = json.loads(template_text)
                policy = ExecutionPolicy(allow_live_site=allow_live,
                    allow_live_probes=enable_live_probes, allow_live_recovery=enable_live_recovery,
                    allow_downloads=allow_downloads, allowed_actions=actions,
                    max_probes=int(probe_on), max_repairs=int(recovery_on)) if engine == "dynamic" else None
                st.session_state["result"] = run_pipeline(
                    requirement=requirement, target=target, mode=mode, engine=engine,
                    headless=headless, demo_delay=demo_delay, allow_live_site=allow_live,
                    only_generate=only_generate, provider=provider, model=model,
                    max_probes=int(probe_on), max_repairs=int(recovery_on),
                    enable_live_probes=enable_live_probes,
                    enable_live_recovery=enable_live_recovery,
                    demo_evidence_gap=injection.startswith("模拟缺失"),
                    demo_broken_locator=injection.startswith("模拟关键词"),
                    execute_dynamic=execute_dynamic, target_url=target_url,
                    fixture_site=fixture_site, template_design=template_design, execution_policy=policy)
            except Exception as e:
                st.error(f"执行未完成：{type(e).__name__}: {e}")
if "result" in st.session_state:
    r = st.session_state["result"]
    v = r["verdict"]
    st.divider()
    st.subheader("运行结果")
    if v["status"] == "PASS":
        st.success(f'PASS · {v["message"]}')
    elif v["status"] == "FAIL":
        st.error(f'FAIL · {v["message"]}')
    else:
        st.warning(f'{v["status"]} · {v["message"]}')
    st.caption("运行目录：" + r["run_dir"] + " · 引擎：" + r.get("engine", engine) +
               " · DOM 来源：" + r.get("inspector", {}).get("inspector_source", "dynamic session"))
    if r.get("evidence", {}).get("error_stage"):
        st.info("Selenium 失败阶段：" + r["evidence"]["error_stage"])
        if r["evidence"].get("diagnostics"):
            with st.expander("浏览器失败现场诊断"):
                st.json(r["evidence"]["diagnostics"])
    stage_cols = st.columns(6)
    for col, s in zip(stage_cols, r["stages"]):
        col.metric(s["agent"], s["result"])
    t1, t2, t3, t4, t5, t6 = st.tabs(["需求/Agent2 契约", "Agent3 生成决策", "DOM 观察", "Selenium 代码", "Agent4～6 证据与恢复", "测试产物"])
    with t1:
        st.write("独立审核结果")
        st.json(r.get("reviews", []))
        st.write("验证结果")
        st.json(v.get("checks", []))
        st.write("原始需求与动态步骤状态")
        st.json({"design": r.get("design"), "step_states": r.get("step_states", {}), "target": r.get("target_config")})
        st.write("冻结的测试契约")
        st.json(r.get("contract"))
    with t2:
        for audit in sorted(Path(r["run_dir"]).glob("generation/*/generation_*.json")):
            with st.expander(str(audit.relative_to(r["run_dir"]))):
                st.json(json.loads(audit.read_text(encoding="utf-8")))
        p = Path(r["run_dir"]) / "generation_audit.json"
        if p.exists():
            st.json(__import__("json").loads(p.read_text(encoding="utf-8")))
        p = Path(r["run_dir"]) / "locator_plan.json"
        if p.exists():
            st.json(__import__("json").loads(p.read_text(encoding="utf-8")))
    with t3:
        for obs in sorted(Path(r["run_dir"]).glob("observation_*.json")):
            with st.expander(obs.name):
                st.json(json.loads(obs.read_text(encoding="utf-8")))
        images = r.get("evidence", {}).get("screenshots", [])
        if not images:
            st.info("此次运行未采集截图；动态模式使用上方 DOM 摘要和结构化证据。")
        for name in images:
            p = Path(r["run_dir"]) / name
            if p.exists():
                st.image(str(p), caption=name)
    with t4:
        sources = list(Path(r["run_dir"]).rglob("dynamic_*.py"))
        if sources:
            for p in sources:
                with st.expander(str(p.relative_to(r["run_dir"]))): st.code(p.read_text(encoding="utf-8"), language="python")
        elif (Path(r["run_dir"]) / "generated_test.py").exists():
            st.code((Path(r["run_dir"]) / "generated_test.py").read_text(encoding="utf-8"), language="python")
    with t5:
        st.write("Agent4 动态证据")
        st.json(r.get("evidence", {}).get("collection_log", r.get("evidence", {})))
        st.write("Agent5 主动补证")
        st.json(r.get("probes", r.get("evidence", {}).get("probes", [])))
        st.write("Agent6 受控恢复 / 运行次数")
        st.json({"attempts": r.get("evidence", {}).get("attempt_history", r.get("steps", [])),
                 "recovery": r.get("recovery_history", v.get("recovery_history", [])),
                 "probe_count": r.get("probe_count", 0),
                 "repair_count": r.get("repair_count", 0)})
    with t6:
        p = Path(r.get("report", r["run_dir"]))
        if p.is_file():
            st.download_button("下载 HTML 报告", p.read_bytes(), file_name="report.html", mime="text/html")
        for name in ["draft_cases.json", "review_decision.json", "test_contract.json", "dom_snapshot.json", "locator_plan.json", "generation_audit.json", "evidence.json", "final_verdict.json", "runner.log", "failure_diagnostics.json", "probe_request.json", "probe_result.json",
                     "repair_request.json", "recovery_history.json", "evidence_attempt2.json",
                     "locator_plan_attempt2.json", "generation_audit_attempt2.json",
                     "controlled_evidence_gap.json", "test_design.json", "dynamic_result.json", "dynamic_execution.json",
                     "dynamic_evidence.json", "execution_attempts.json", "dynamic_probes.json", "dynamic_recovery.json"]:
            item = Path(r["run_dir"]) / name
            if item.exists():
                with st.expander(name):
                    st.code(item.read_text(encoding="utf-8")[:15000])
