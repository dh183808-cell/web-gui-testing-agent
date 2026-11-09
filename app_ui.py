"""Streamlit presentation of the existing Dynamic Pipeline; no execution logic."""
from html import escape
import json
import uuid

import streamlit as st

from web_agent.ui_tasks import (STAGE_LABELS, PROVIDER_LABELS, TaskStore, artifact_path,
                                progress_view, read_json, result_view, provider_defaults)
from web_agent.llm_adapter import StructuredLLM, public_config, safe_model_error


st.set_page_config(page_title='WebAgent · 测试操作台', page_icon='🧪', layout='wide')
st.markdown('''<style>
.block-container{max-width:1660px;padding-top:1.4rem;padding-bottom:2rem}
h1{font-size:1.85rem!important;padding-bottom:.3rem!important}
h2{font-size:1.4rem!important} h3{font-size:1.1rem!important}
[data-testid="stMetricValue"]{font-size:1.5rem}
.stage{border:1px solid #dce3ed;border-radius:10px;padding:10px 13px;margin:3px 0 8px;background:#f8fafc;min-height:83px}
.stage.active{border-color:#2a64bc;background:#edf4ff;box-shadow:inset 3px 0 #2a64bc}
.stage.done{border-color:#bbdaca;background:#f4faf6}
.stage.error{border-color:#e4b4b4;background:#fff3f2}
.stage.review{border-color:#ddca9b;background:#fffaf0}
.stage strong{font-size:14px;color:#18354f;display:block;margin-bottom:6px}
.stage span{font-size:13px;color:#526477}
.result-title{font-size:29px;font-weight:750;line-height:1.3;margin:8px 0 5px}
.muted{color:#66758a;font-size:14px}
.oracle-value{border:1px solid #dce3ed;border-radius:8px;padding:10px 13px;background:#f8fafc;overflow-wrap:anywhere;white-space:pre-wrap;font-size:19px}
</style>''', unsafe_allow_html=True)


@st.cache_resource
def task_store():
    return TaskStore()


store = task_store()
if st.session_state.pop('clear_api_key', False): st.session_state.pop('api_key', None)
active_id = store.active()
selected_id = st.query_params.get('job') or active_id
selected = store.snapshot(selected_id) if selected_id else None
if selected and st.query_params.get('job') != selected_id: st.query_params['job'] = selected_id


def select_history():
    key = st.session_state['history_job']
    if key: st.query_params['job'] = key


def new_task():
    st.query_params.clear()
    st.session_state['request_id'] = uuid.uuid4().hex
    st.session_state['authorized'] = False
    st.session_state['history_job'] = None
    st.session_state.pop('displayed_job', None)


with st.sidebar:
    st.markdown('### WebAgent v0.6')
    st.caption('测试操作台 · 本地单任务服务')
    if active_id:
        st.info('有一个任务正在执行')
        if st.button('返回正在运行的任务', key='return_active'):
            st.query_params['job'] = active_id
            st.rerun()
    st.button('新建测试任务', key='new_task', on_click=new_task, disabled=bool(active_id))
    recent = store.recent()
    labels = {row['id']: row['label'] for row in recent}
    options = [None, *labels]
    if selected_id in labels:
        st.session_state['history_job'] = selected_id
    elif st.session_state.get('history_job') not in options:
        st.session_state['history_job'] = None
    st.selectbox('最近任务 · 只读查看', options, key='history_job',
                 format_func=lambda key: labels.get(key, '选择历史任务'), on_change=select_history)
    st.caption('查看记录不会调用模型或启动浏览器。')
    st.divider()
    st.caption('编写调试与完整模拟运行：后台执行\n\n正式测试：独立可见 Chrome\n\n刷新后继续查看同一任务。')

st.title('WebAgent · Web GUI 测试操作台')
if selected:
    st.caption(('正在执行' if selected['status'] == 'RUNNING' else '历史任务 · 只读')
               + '  /  ' + selected_id)
else:
    st.caption('配置测试 → 六 Agent 执行 → 独立正式取证 → 对照冻结预期判定')
if selected_id and not selected:
    st.warning('未找到任务记录。请从左侧选择历史任务或新建任务；不会自动重新执行。')


def change_provider():
    st.session_state.update(provider_defaults(st.session_state['ai_provider']))
    st.session_state['api_key'] = ''
    st.session_state.pop('connection_result', None)


def configure():
    if 'model' not in st.session_state: st.session_state.update(provider_defaults('deepseek'))
    st.session_state.setdefault('request_id', uuid.uuid4().hex)
    with st.container(border=True):
        st.subheader('配置本次测试')
        cols = st.columns(3)
        with cols[0]: st.selectbox('AI 服务商', list(PROVIDER_LABELS), format_func=PROVIDER_LABELS.get,
                                   key='ai_provider', on_change=change_provider)
        with cols[1]: st.text_input('模型 ID（可编辑）', key='model')
        with cols[2]: st.text_input('API Key（仅本次任务）', type='password', key='api_key')
        with st.expander('AI 高级设置与连接检查', expanded=False):
            st.text_input('API Base URL', key='base_url')
            st.selectbox('JSON 输出模式', ['auto', 'on', 'off'], key='json_mode',
                help='关闭接口 JSON 模式仍严格校验 JSON 和 Agent 契约。部分兼容模型只支持提示词 JSON。')
            st.caption('模型和地址可从 .env 的公开配置读取；密钥不会自动填入。连接检查会发送一次小型模型请求。')
            if st.button('测试模型连接', key='test_connection', disabled=not st.session_state.get('api_key', '').strip()):
                client = None
                try:
                    config = public_config(st.session_state['ai_provider'], st.session_state['model'],
                                           st.session_state['base_url'], st.session_state['json_mode'])
                    client = StructuredLLM(**config, api_key=st.session_state['api_key'], use_environment=False)
                    with st.spinner('正在检查连接与 JSON 输出…'):
                        answer = client.complete_json('Return JSON {"ok":true}.', 'Connection test only.')
                    if answer.get('ok') is not True: raise ValueError('Connection JSON schema mismatch')
                    st.success('连接成功，模型能够返回有效 JSON；不代表完整测试一定可执行。')
                except Exception as exc: st.error(safe_model_error(exc))
                finally:
                    if client: client.close()
        st.text_input('目标 URL', key='url')
        left, right = st.columns(2)
        with left: st.text_area('Test Basis · 测试依据（可选）', key='test_basis', height=145)
        with right: st.text_area('Test Requirement · 测试需求', key='requirement', height=145)
        st.slider('正式执行页面停留时间（秒）', 0.0, 3.0, 1.5, .5, key='formal_demo_delay',
                  help='正式取证完成后停留展示；对时间敏感的网站请设为 0。后台调试不延时。')
        consent = st.checkbox('我授权此次站点测试与独立重跑，并确认使用可安全重复的测试数据', key='authorized')
        required = dict(api_key='API Key', model='模型 ID', url='目标 URL', requirement='测试需求')
        missing = [label for key, label in required.items() if not st.session_state.get(key, '').strip()]
        ready = consent and not missing
        if missing: st.caption('开始前请填写：' + '、'.join(missing))
        if st.button('开始测试', key='start_test', type='primary', disabled=not ready or bool(active_id)):
            try:
                job_id = store.start({key: st.session_state[key] for key in ('url', 'test_basis', 'requirement')},
                                     request_id=st.session_state['request_id'],
                                     ai_config=public_config(st.session_state['ai_provider'], st.session_state['model'],
                                         st.session_state['base_url'], st.session_state['json_mode']),
                                     api_key=st.session_state['api_key'], formal_demo_delay=st.session_state['formal_demo_delay'])
                st.session_state['clear_api_key'] = True
                st.query_params['job'] = job_id
                st.rerun()
            except (ValueError, KeyError) as exc: st.error(str(exc))
    with st.expander('使用说明'):
        st.caption('未填写 Key 不会使用后台密钥。任务结束后请重新填写。仅测试获准且可重复操作的环境；本地受限解释器不是操作系统沙箱。')


def readonly_config(job):
    with st.expander('本次测试配置 · 只读', expanded=False):
        st.text('URL：' + job['inputs']['url'])
        st.write('测试依据'); st.text(job['inputs'].get('test_basis') or '未提供')
        st.write('测试需求'); st.text(job['inputs']['requirement'])
        config = job.get('ai_config', {})
        if config: st.text('AI：' + PROVIDER_LABELS.get(config['provider'], config['provider']) + ' / ' + config['model'])
        if 'formal_demo_delay' in job: st.text(f"正式页面停留：{job['formal_demo_delay']:g} 秒")


def stage_cards(view):
    names = ['Agent1', 'Agent2', 'Agent3', 'Cold', 'Formal', 'Agent5']
    if view['stages']['Agent6']['called']: names.append('Agent6')
    for offset in range(0, len(names), 3):
        for col, name in zip(st.columns(3), names[offset:offset+3]):
            state = view['stages'][name]
            style = {'执行中': 'active', '已完成': 'done', '异常': 'error', '审核未通过': 'review'}.get(state['status'], '')
            clock = f"{state['seconds']:.1f}s" if state['seconds'] is not None else '—'
            col.markdown(f'<div class="stage {style}"><strong>{escape(STAGE_LABELS[name])}</strong>'
                         f'<span>{escape(state["status"])} · {clock}</span></div>', unsafe_allow_html=True)


def progress_panel(job):
    view = progress_view(job)
    running = job['status'] == 'RUNNING'
    current = STAGE_LABELS.get(view['current'], view['current'])
    st.subheader(('正在执行 · ' + current) if running else '执行记录')
    config = job.get('ai_config', {})
    if config: st.caption(PROVIDER_LABELS.get(config['provider'], config['provider']) + ' / ' + config['model'])
    cols = st.columns([1.25, 1, 1.25, 1, 1])
    values = [view['step'], f"{view['elapsed']:.1f}s", f"{view['calls_finished']} / {view['calls']}",
              f"{view['current_seconds'] or 0:.1f}s" if running else '已结束', f"{view['repairs']} / 5"]
    for col, title, value in zip(cols, ['当前步骤', '总耗时', 'LLM 已结束 / 已发起', '当前阶段耗时', '失败修复'], values): col.metric(title, value)
    stage_cards(view)
    if view['pending_seconds'] is not None:
        config = job.get('ai_config', {})
        st.info(f"正在等待 {PROVIDER_LABELS.get(config.get('provider'), '模型')} 返回 · 本次已等待 {view['pending_seconds']:.1f} 秒 · "
                f"{current} · 未结束调用 {view['calls']-view['calls_finished']} 个")
    elif view['formal_open']:
        st.info('正式浏览器已启动 · Agent4 正在独立执行 ' + view['step'] + ' · 结束后自动关闭')
    elif running:
        latest = view['activities'][-1]['text'] if view['activities'] else '等待首条执行事件'
        st.info('当前活动：' + latest)
    with st.container(border=True):
        st.markdown('**最新活动**')
        for item in reversed(view['activities'][-5:]):
            st.text(f"{item['seconds']:6.1f}s  {item['text']}")
    readonly_config(job)
    with st.expander('完整原始事件日志', expanded=False):
        st.caption('阶段耗时根据事件归属累计，包含等待；Agent4 与 Formal 描述同一段正式执行。模型计数不含 SDK 内部重试。')
        st.json(job.get('events', []), expanded=False)
        st.download_button('下载事件日志', json.dumps(job.get('events', []), ensure_ascii=False, indent=2),
                           file_name='events.json', key='events_' + job['id'])


def format_value(value):
    return json.dumps(value, ensure_ascii=False, indent=2)


def show_results(job):
    view = result_view(job)
    verdict, rows = view['verdict'], view['rows']
    status = verdict['status']
    color = {'PASS': '#20704b', 'FAIL': '#b63636', 'ERROR': '#b63636', 'INCONCLUSIVE': '#97610c'}.get(status, '#18354f')
    meaning = {'PASS': '正式行为符合冻结预期', 'FAIL': '正式证据确认与冻结预期不符',
               'ERROR': '技术执行未能完成', 'INCONCLUSIVE': '尚不能可靠判定'}
    st.markdown(f'<div class="result-title" style="color:{color}">{escape(status)} · {meaning.get(status, "测试结果")}</div>', unsafe_allow_html=True)
    pv = progress_view(job)
    st.caption(f"总耗时 {pv['elapsed']:.1f}s  ·  模型调用 {pv['calls']} 次  ·  失败修复 {pv['repairs']} 次  ·  {view['execution_summary']}")
    if verdict.get('error_type'): st.error(verdict['error_type'] + ' · ' + verdict.get('failed_stage', ''))
    if job.get('error_message'): st.error(job['error_message'])
    for event in reversed(job.get('events', [])):
        if event.get('error_message'):
            st.error(event['error_message'])
            break
    if job['status'] == 'INTERRUPTED': st.warning('服务重启后任务中断；未自动恢复或重跑。')
    for issue in verdict.get('issues', []): st.write(issue)
    if rows:
        mode = st.radio('正式证据查看方式', ['按验证项', '按执行步骤'], horizontal=True, key='evidence_mode_' + job['id'])
        if mode == '按验证项':
            options = [row['oracle_id'] for row in rows]
            focus = view['focus']['oracle_id']
            oid = st.selectbox('选择验证项 · 默认优先关键结论', options, index=options.index(focus),
                format_func=lambda key: next(f"{r['oracle_id']} / {r['step_id']} · {r['status']} · {r['meaning']}" for r in rows if r['oracle_id'] == key),
                key='oracle_' + job['id'])
            row = next(r for r in rows if r['oracle_id'] == oid)
            left, right = st.columns([.85, 1.15], gap='large')
            with left:
                st.subheader(row['oracle_id'] + ' · ' + row['status'])
                st.write(row['meaning'])
                st.caption({'test_basis': '来自测试依据', 'user_requirement': '来自用户验收条件'}.get(row['source_type'], row['source_type']))
                st.text(row['source_quote'] or '未记录来源引文')
                st.markdown('**冻结预期**')
                st.markdown('<div class="oracle-value">' + escape(format_value(row['expected'])) + '</div>', unsafe_allow_html=True)
                st.markdown('**正式实际值**')
                actual = format_value(row['actual']) if row['actual_available'] else '未执行，未采集实际值' if row['status'] == 'NOT_EXECUTED' else '缺少正式测量值'
                st.markdown('<div class="oracle-value">' + escape(actual) + '</div>', unsafe_allow_html=True)
                if row['status'] == 'NOT_EXECUTED': st.info('前置条件阻塞：' + (row['blocked_by'] or '详见执行记录') + '。此项未验证，不作为已证实失败。')
            with right:
                st.markdown('**对应 Agent4 正式截图**')
                if row['screenshot']:
                    st.image(str(row['screenshot']), width='stretch', caption=row['oracle_id'] + ' / ' + row['step_id'] + ' · 正式取证')
                    st.download_button('下载当前正式截图', row['screenshot'].read_bytes(), file_name=row['screenshot'].name, key='shot_' + job['id'])
                else: st.info('该验证项没有可用的正式截图。')
        else:
            steps = view['steps']
            if steps:
                sid = st.selectbox('选择正式执行步骤', [s['step_id'] for s in steps], key='step_' + job['id'])
                step = next(s for s in steps if s['step_id'] == sid)
                st.write(sid + ' · ' + step['status']); st.caption(step['current_url'])
                if step['screenshot']: st.image(str(step['screenshot']), width='stretch', caption=sid + ' · 正式取证')
                else: st.info('此步骤未保存正式截图。')
            else: st.info('没有正式步骤记录。')
        with st.expander('全部验证项对比', expanded=False):
            st.dataframe([{'验证项': r['oracle_id'], '步骤': r['step_id'], '判定': r['status'],
                '测试依据 / 验收条件': r['source_quote'], '冻结预期': format_value(r['expected']),
                '正式实际值': format_value(r['actual']) if r['actual_available'] else '未采集'} for r in rows],
                hide_index=True, width='stretch')
    else: st.info(view['execution_summary'])
    if view['skipped']:
        with st.container(border=True):
            st.markdown('**未执行的依赖步骤 · NOT_EXECUTED**')
            for item in view['skipped']:
                st.write(item['step_id'] + '：被 ' + item['blocked_by'] + ' 阻塞，后续依赖操作未执行。')
            st.caption('未执行不等于已验证失败；总 FAIL 来自前置验证项的可靠正式证据。')
    with st.expander('代码、报告与完整 JSON 证据', expanded=False):
        code, report, evidence = st.tabs(['冻结 Selenium 代码', '报告', '完整证据'])
        run = view['run_dir']
        with code:
            path = artifact_path(run, 'generated_test.py')
            if path:
                st.code(path.read_text(encoding='utf-8'), language='python')
                st.download_button('下载生成代码', path.read_bytes(), file_name='generated_test.py')
            else: st.info('尚未交付脚本。')
        with report:
            path = artifact_path(run, 'report.md')
            if path:
                st.text(path.read_text(encoding='utf-8'))
                st.download_button('下载报告', path.read_bytes(), file_name='report.md', mime='text/markdown')
        with evidence:
            st.caption('运行目录：' + str(run))
            for name in ('test_inputs.json', 'test_contract.json', 'agent3_delivery.json', 'formal_evidence.json',
                         'final_verdict.json', 'cleanup.json', 'llm_calls.json', 'agent3_debug.json'):
                path = artifact_path(run, name)
                if path:
                    with st.expander(name):
                        st.json(read_json(path, {}), expanded=False)
                        st.download_button('下载 ' + name, path.read_bytes(), file_name=name, key='file_' + name)


def render_task(job_id):
    job = store.snapshot(job_id)
    if not job: return None
    if job['status'] == 'RUNNING': progress_panel(job)
    else:
        show_results(job)
        readonly_config(job)
        with st.expander('阶段时间线与完整活动记录', expanded=False): progress_panel(job)
    return job['status']


@st.fragment(run_every='1s')
def live_task(job_id):
    if render_task(job_id) != 'RUNNING': st.rerun()


if selected:
    if selected['status'] == 'RUNNING': live_task(selected_id)
    else: render_task(selected_id)
elif not selected_id: configure()
