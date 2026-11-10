"""Deterministic dates/provenance and synthetic evidence tests, NOT live E2E."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from web_agent.date_semantics import normalize_date, validate_date_source
from web_agent.schema import (attach_oracle_sources, oracle_source_issues,
                              validate_dynamic_plan, validate_oracle_binding)
from web_agent.agents.designer import Agent1TestDesigner
from web_agent.agents.reviewer import Agent2TestReviewer
from web_agent.agents.verifier import Agent5EvidenceVerifier
from web_agent.checked_selenium import collect_measurements, normalize_bindings, validate_source
from web_agent.dom_inspector import compact_dom


REQUIREMENT = '在页面中找到Date picker，选择日期为2026年11月10日'
BASIS = '找到 Date picker 的字样，点击旁边的输入框，在弹出的日历弹窗中可以选择日期。'


def plan(quote='选择日期为2026年11月10日', expected='2026-11-10', requirement=REQUIREMENT):
    result = dict(requirement=requirement, test_basis=BASIS, provenance_version=1,
        steps=[dict(id='S1', goal='点击相邻输入框打开日历'),
               dict(id='S2', goal='观察日历并选择目标日期', depends_on=['S1'])],
        oracles=[dict(id='O1', step_id='S2', meaning='所选日期正确', kind='date_equals',
            expected=expected, source_type='user_requirement', source_quote=quote)],
        preconditions=[], missing_inputs=[])
    attach_oracle_sources(result)
    return result


@pytest.mark.parametrize('raw,expected', [('2026年11月10日','2026-11-10'),
    ('2026 年 1 月 2 日','2026-01-02'), ('2026-11-10','2026-11-10'),
    ('2024年2月29日','2024-02-29'), ('2000-02-29','2000-02-29')])
def test_unambiguous_calendar_dates(raw, expected):
    assert normalize_date(raw) == expected


@pytest.mark.parametrize('raw', ['2026-02-30','2026-02-29','1900-02-29','2026年13月1日',
    '2026-00-10','2026-11-00','0000-01-01','2026-11-31','03/04/2026',
    '2026-1-2','2026-11-10T00:00:00', None, True])
def test_invalid_or_ambiguous_dates_never_roll_over(raw):
    with pytest.raises(ValueError):
        normalize_date(raw)


@pytest.mark.parametrize('quote,expected', [('选择日期为2026年11月10日','2026-11-10'),
    ('选择2026-11-10','2026-11-10'), ('日期2024年2月29日','2024-02-29')])
def test_date_source_equivalence_and_exact_span(quote, expected):
    draft = plan(quote, expected, '需求\n' + quote)
    validate_dynamic_plan(draft)
    assert oracle_source_issues(draft) == []
    assert draft['oracles'][0]['source_span'] == dict(start=3,end=len(draft['requirement']),line=2)


@pytest.mark.parametrize('quote,expected', [('选择2026年11月10日','2026-11-11'),
    ('选择2026-02-30','2026-03-02'), ('选择03/04/2026','2026-03-04'),
    ('选择日期','2026-11-10'), ('从2026-11-10到2026-11-11','2026-11-10'),
    ('2026年11月10日或03/04/2026','2026-11-10'),
    ('选择2026年11月10日','2026年11月10日')])
def test_unsupported_mismatching_and_multi_date_sources_are_rejected(quote, expected):
    with pytest.raises(ValueError):
        validate_date_source(expected, quote)


def test_date_does_not_bypass_citation_span_or_version_checks():
    draft = plan()
    for change in [dict(source_quote='不存在的引用'), dict(source_type='page'),
                   dict(source_span=dict(start=0,end=1,line=1)), dict(source_quote='')]:
        broken = deepcopy(draft)
        broken['oracles'][0].update(change)
        with pytest.raises(ValueError, match='source'):
            validate_dynamic_plan(broken)
    unversioned = deepcopy(draft)
    unversioned.pop('provenance_version')
    unversioned['oracles'][0].pop('source_span')
    with pytest.raises(ValueError, match='source_span'):
        validate_dynamic_plan(unversioned)


def test_only_date_kind_normalizes_and_legacy_contract_stays_readable():
    draft = plan()
    draft['oracles'][0]['kind'] = 'equals'
    with pytest.raises(ValueError, match='literal expected'):
        validate_dynamic_plan(draft)
    legacy = dict(steps=[dict(id='S1',goal='检查旧文本')],
                  oracles=[dict(id='O1',step_id='S1',kind='equals',expected='Received!',meaning='旧契约')])
    validate_dynamic_plan(json.loads(json.dumps(legacy)))


def test_explicit_display_and_calendar_date_remain_separate_expectations():
    requirement = '选择2026年11月10日，并验证输入框显示为11/10/2026。'
    draft = plan('选择2026年11月10日',requirement=requirement)
    draft['oracles'].append(dict(id='O2',step_id='S2',meaning='明确指定的显示字符串',
        kind='equals',expected='11/10/2026',source_type='user_requirement',source_quote='显示为11/10/2026'))
    attach_oracle_sources(draft)
    validate_dynamic_plan(draft)
    # A text input displays its raw value. A native date input's localized
    # display cannot be inferred from its ISO DOM value.
    evidence = synthetic_evidence('2026-11-10')
    evidence['measurements']['O2'] = dict(evidence['measurements']['O1'])
    verdict = Agent5EvidenceVerifier().verify_dynamic(draft,evidence)
    assert [c['passed'] for c in verdict.checks] == [True,False]
    assert verdict.status == 'FAIL'  # Date equality cannot hide an explicit format mismatch.


class ApprovingModel:
    """Only tests controller gates/prompt contracts, not real LLM understanding."""
    def __init__(self, draft):
        self.draft, self.prompts = draft, []
    def complete_json(self, system, user):
        self.prompts.append(system)
        if system.startswith('You are Agent1'):
            return deepcopy(self.draft)
        return dict(approved=True, input_status='consistent', issues=[], coverage=['O1'])


def test_date_planner_and_review_accept_semantics_without_assumed_named_target():
    model = ApprovingModel(plan())
    draft = Agent1TestDesigner().design_dynamic(REQUIREMENT, url='https://authorized.test/',
                                                test_basis=BASIS, llm=model)
    assert Agent2TestReviewer().review_dynamic(draft,llm=model)['approved']
    assert 'target' not in draft['oracles'][0]
    assert all('date_equals' in p and 'accessible name' in p for p in model.prompts)
    assert draft['oracles'][0]['expected'] == '2026-11-10'


def test_date_rejection_still_uses_single_planning_revision(tmp_path, monkeypatch):
    from tests.test_test_basis import run_plan
    model = ApprovingModel(plan(expected='2026-11-11'))
    result, path = run_plan(tmp_path,monkeypatch,model,REQUIREMENT,BASIS)
    assert result['verdict']['status'] == 'INCONCLUSIVE'
    assert result['verdict']['reason_type'] == 'contract_unapproved'
    assert sum(p.startswith('You are Agent1') for p in model.prompts) == 2
    assert json.loads((path/'review_revisions.json').read_text('utf-8'))['planning'] == 1
    assert not (path/'agent3_delivery.json').exists()


def synthetic_evidence(raw, fmt=None, *, purpose='formal', native=False):
    attrs = {'data-date-format':fmt} if fmt else {}
    measurement = dict(read='attribute',attribute='value',by='id',selector='date',step_id='S2',
        value=raw,status='observed',visible=1,matched=1,
        observation_ref='synthetic-observation.json',screenshot_ref='synthetic-screenshot.png',
        date_context=dict(tag='input',type='date' if native else 'text',format_attributes=attrs))
    return dict(execution_status='COMPLETE',purpose=purpose,measurements={'O1':measurement})


@pytest.mark.parametrize('raw,fmt,status', [('2026-11-10',None,'PASS'),
    ('11/10/2026','MM/DD/YYYY','PASS'), ('10/11/2026','DD/MM/YYYY','PASS'),
    ('11/11/2026','mm/dd/yyyy','FAIL'), ('03/04/2026',None,'INCONCLUSIVE'),
    ('2026-02-30',None,'INCONCLUSIVE'), ('02/30/2026','MM/DD/YYYY','INCONCLUSIVE'),
    ('2026年11月10日',None,'PASS'), ('',None,'INCONCLUSIVE')])
def test_synthetic_formal_date_comparison(raw, fmt, status):
    evidence = synthetic_evidence(raw,fmt)
    before = deepcopy(evidence)
    verdict = Agent5EvidenceVerifier().verify_dynamic(plan(),evidence)
    assert verdict.status == status
    check = verdict.checks[0]
    assert check['actual'] == raw and check['expected'] == '2026-11-10'
    assert check['date_comparison']['raw_actual'] == raw
    if status == 'PASS':
        assert check['date_comparison']['normalized_actual'] == '2026-11-10'
    assert evidence == before


@pytest.mark.parametrize('fault', ['missing','trial','no-purpose','no-screenshot','wrong-step',
                                  'hidden','conflicting-format','wrong-native-value','model-format',
                                  'no-visibility','no-step','ambiguous-node'])
def test_date_needs_complete_independent_formal_evidence(fault):
    evidence = synthetic_evidence('2026-11-10')
    m = evidence['measurements']['O1']
    if fault == 'missing': evidence['measurements'] = {}
    elif fault == 'trial': evidence['purpose'] = 'trial'
    elif fault == 'no-purpose': evidence.pop('purpose')
    elif fault == 'no-screenshot': m.pop('screenshot_ref')
    elif fault == 'wrong-step': m['step_id'] = 'S1'
    elif fault == 'hidden': m['visible'] = 0
    elif fault == 'no-visibility': m.pop('visible')
    elif fault == 'no-step': m.pop('step_id')
    elif fault == 'ambiguous-node': m['matched'] = 2
    elif fault == 'conflicting-format':
        m['value'] = '11/10/2026'
        m['date_context']['format_attributes'] = {'data-format':'DD/MM/YYYY','placeholder':'MM/DD/YYYY'}
    elif fault == 'wrong-native-value':
        m.update(value='11/10/2026',date_context=dict(tag='input',type='date'))
    elif fault == 'model-format':
        m.update(value='11/10/2026',format='MM/DD/YYYY')
    assert Agent5EvidenceVerifier().verify_dynamic(plan(),evidence).status == 'INCONCLUSIVE'


def test_native_value_is_iso_and_execution_fault_is_error():
    evidence = synthetic_evidence('2026-11-10',native=True)
    verifier = Agent5EvidenceVerifier()
    assert verifier.verify_dynamic(plan(),evidence).status == 'PASS'
    evidence['execution_status'] = 'ERROR'
    assert verifier.verify_dynamic(plan(),evidence).status == 'ERROR'


def test_text_feedback_still_compares_strictly():
    contract = dict(oracles=[dict(id='O1',step_id='S2',kind='equals',expected='Accepted!',meaning='反馈')])
    evidence = synthetic_evidence('Received!')
    assert Agent5EvidenceVerifier().verify_dynamic(contract,evidence).status == 'FAIL'


def test_date_bindings_cannot_supply_formats_or_change_read_type():
    binding = dict(by='id',selector='date',read='attribute',attribute='value',step_id='S2')
    oracle = plan()['oracles'][0]
    validate_oracle_binding(oracle,normalize_bindings({'O1':binding})['O1'])
    for key in ('date_context','format','expected'):
        with pytest.raises(ValueError, match='unsupported fields'):
            normalize_bindings({'O1':dict(binding,**{key:'MM/DD/YYYY'})})
    for change in (dict(read='count'),dict(attribute='placeholder')):
        with pytest.raises(ValueError):
            validate_oracle_binding(oracle,dict(binding,**change))
    with pytest.raises(ValueError, match='Unsupported Selenium member'):
        validate_source('driver.execute_script("arguments[0].value = 1")')


def test_trusted_attribute_collector_captures_actual_node_context():
    row = dict(value='10/11/2026',tag='input',type='text',format_attributes={'data-format':'DD/MM/YYYY'})
    node = object()
    def script(source, *args):
        if 'format_attributes:Object.fromEntries' in source:
            assert args[0] is node
            return deepcopy(row)
        return [dict(visible=True,sensitive=False)]
    driver = SimpleNamespace(find_elements=lambda *a:[node],execute_script=script)
    result = collect_measurements(driver,'https://authorized.test/',
        {'O1':dict(selector='#date',read='attribute',attribute='value',step_id='S2')},batch=True)['O1']
    assert result['value'] == row['value']
    assert result['date_context']['format_attributes'] == row['format_attributes']
    assert 'expected' not in result


def test_dom_keeps_format_and_calendar_candidates_without_asserting_names():
    html = '<label>Date picker</label><input id="d" data-date-format="dd/mm/yyyy">' \
           '<table><tr><th class="next">Next</th></tr><tr><td data-date="123">10</td></tr></table>'
    dom = compact_dom(html)
    assert 'data-date-format="dd/mm/yyyy"' in dom['html']
    assert 'data-date="123"' in dom['calendar_regions'][0]
    assert dom['elements'][0]['aria_label'] == ''
