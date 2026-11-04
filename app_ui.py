"""Streamlit UI for the six-agent arXiv demo.

Run: streamlit run app_ui.py
"""
from pathlib import Path

import streamlit as st
from web_agent.pipeline import DEFAULT_REQUIREMENT, run_pipeline

st.set_page_config(page_title="Web GUI Testing Agent · v0.3", page_icon="🧪", layout="wide")
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
st.markdown('<div class="hero"><h1>Web GUI Testing Agent <span style="font-size:17px">v0.3</span></h1>'
            '<p>arXiv 研究检索 · 六角色协作 · Selenium 执行 · 证据驱动验证</p></div>',
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
    target_label = st.selectbox("被测环境", ["本地 arXiv 风格仿真页（正常）", "本地 arXiv 风格仿真页（受控缺陷）", "真实 arxiv.org（需符合站点规则）"])
    target = ["fixture", "fixture-bug", "arxiv"][
        ["本地 arXiv 风格仿真页（正常）", "本地 arXiv 风格仿真页（受控缺陷）", "真实 arxiv.org（需符合站点规则）"].index(target_label)]
with right:
    st.subheader("执行配置")
    mode_label = st.radio("Agent1/2/3 运行模式", ["模板演示（无需 API Key）", "真实 LLM（DeepSeek / OpenRouter / OpenAI）"], index=0)
    mode = "llm" if mode_label.startswith("真实") else "template"
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
    injection = "无"
    if target != "arxiv" and not only_generate:
        injection = st.selectbox("演示故障注入（仅本地仿真页）",
            ["无", "模拟缺失详情标题证据（Agent5）", "模拟关键词定位器失效（Agent6）"])
    enable_live_probes = enable_live_recovery = False
    if target == "arxiv":
        st.caption("真实站点默认不会自动重复访问或修复重跑，需要额外单独确认。")
        enable_live_probes = st.checkbox("单独允许一次真实站点定向补证", value=False)
        enable_live_recovery = st.checkbox("单独允许一次真实站点定位故障恢复重跑", value=False)
    allow_live = st.checkbox("我已确认目标站点允许此次低频测试，不做批量抓取或绕过访问限制", value=False) if target == "arxiv" else False
    st.caption("模板模式：固定打开论文详情，关键词从引号提取。LLM 模式：支持只搜索/进入详情、字段筛选和计算机科学分类；不支持任意网站或任意任务。")
run = st.button("▶ 开始六 Agent 测试", type="primary", use_container_width=True)
if run:
    if target == "arxiv" and not allow_live:
        st.error("请先确认目标网站自动化访问符合适用规则。")
    else:
        with st.spinner("正在构建测试契约、生成 Selenium 脚本并处理执行证据…"):
            try:
                st.session_state["result"] = run_pipeline(
                    requirement=requirement, target=target, mode=mode,
                    headless=headless, demo_delay=demo_delay, allow_live_site=allow_live,
                    only_generate=only_generate, provider=provider, model=model,
                    max_probes=int(probe_on), max_repairs=int(recovery_on),
                    enable_live_probes=enable_live_probes,
                    enable_live_recovery=enable_live_recovery,
                    demo_evidence_gap=injection.startswith("模拟缺失"),
                    demo_broken_locator=injection.startswith("模拟关键词"))
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
    st.caption("运行目录：" + r["run_dir"] + " · DOM 来源：" + r["inspector"].get("inspector_source", "unknown"))
    if r["evidence"].get("error_stage"):
        st.info("Selenium 失败阶段：" + r["evidence"]["error_stage"])
        if r["evidence"].get("diagnostics"):
            with st.expander("浏览器失败现场诊断"):
                st.json(r["evidence"]["diagnostics"])
    stage_cols = st.columns(6)
    for col, s in zip(stage_cols, r["stages"]):
        col.metric(s["agent"], s["result"])
    t1, t2, t3, t4, t5, t6 = st.tabs(["Agent2 审核与预言", "Agent3 生成决策", "浏览器截图", "Selenium 代码", "Agent4～6 证据与恢复", "测试产物"])
    with t1:
        st.write("独立审核结果")
        st.json(r.get("reviews", []))
        st.write("验证结果")
        st.json(v["checks"])
        st.write("冻结的测试契约")
        st.json(r["contract"])
    with t2:
        p = Path(r["run_dir"]) / "generation_audit.json"
        if p.exists():
            st.json(__import__("json").loads(p.read_text(encoding="utf-8")))
        p = Path(r["run_dir"]) / "locator_plan.json"
        if p.exists():
            st.json(__import__("json").loads(p.read_text(encoding="utf-8")))
    with t3:
        images = r["evidence"].get("screenshots", [])
        if not images:
            st.info("没有可用截图。检查 runner.log 是否为浏览器依赖或网络错误。")
        for name in images:
            p = Path(r["run_dir"]) / name
            if p.exists():
                st.image(str(p), caption=name)
    with t4:
        p = Path(r["run_dir"]) / "generated_test.py"
        st.code(p.read_text(encoding="utf-8"), language="python")
    with t5:
        st.write("Agent4 按 Evidence Plan 采集的记录")
        st.json(r["evidence"].get("collection_log", []))
        st.write("Agent5 主动补证")
        st.json(r["evidence"].get("probes", []))
        st.write("Agent6 受控恢复 / 运行次数")
        st.json({"attempts": r["evidence"].get("attempt_history", []),
                 "recovery": v.get("recovery_history", []),
                 "probe_count": r.get("probe_count", 0),
                 "repair_count": r.get("repair_count", 0)})
    with t6:
        p = Path(r["report"])
        st.download_button("下载 HTML 报告", p.read_bytes(), file_name="report.html", mime="text/html")
        for name in ["draft_cases.json", "review_decision.json", "test_contract.json", "dom_snapshot.json", "locator_plan.json", "generation_audit.json", "evidence.json", "final_verdict.json", "runner.log", "failure_diagnostics.json", "probe_request.json", "probe_result.json",
                     "repair_request.json", "recovery_history.json", "evidence_attempt2.json",
                     "locator_plan_attempt2.json", "generation_audit_attempt2.json",
                     "controlled_evidence_gap.json"]:
            item = Path(r["run_dir"]) / name
            if item.exists():
                with st.expander(name):
                    st.code(item.read_text(encoding="utf-8")[:15000])
