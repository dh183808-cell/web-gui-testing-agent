"""Pure semantic checks using explicit synthetic evidence; not a Formal/E2E run."""
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from tests.test_date_semantics import plan, synthetic_evidence
from web_agent.agents.verifier import Agent5EvidenceVerifier
from web_agent.checked_selenium import normalize_bindings
from web_agent.dom_inspector import compact_dom


def calendar_evidence(day=10, raw='11/10/2026', method='aria-controls'):
    evidence = synthetic_evidence(raw)
    evidence.update(session_id='synthetic-formal-session',browser_session_id='synthetic-browser')
    m = evidence['measurements']['O1']
    m.update(evidence_id='synthetic-measurement')
    m['date_context']['input_ref'] = '/html[1]/body[1]/input[1]'
    root_ref = '/html[1]/body[1]/div[1]'
    snapshot = dict(version=1,status='observed',reasons=[],input_ref=m['date_context']['input_ref'],
        input_value=raw,captured_at='2026-11-09T08:00:00.000Z',
        association=dict(method=method,input_ref=m['date_context']['input_ref'],root_ref=root_ref,
            controlled_id='calendar',owner_count=1,exact_input=True,exact_picker=True),
        root=dict(ref=root_ref,id='calendar',visible=True,grid_ref=root_ref+'/table[1]',
                  grid_visible=True,candidate_count=1),
        heading=dict(text='November 2026',ref=root_ref+'/h2[1]'),selected_count=1,
        selected=[dict(ref=root_ref+'/table[1]/td[1]',text=str(day),visible=True,disabled=False,
            adjacent=False,role='gridcell',aria_selected='true',aria_current='',
            state_basis='aria-selected',data_date='',**{'class':''})],
        component_dates=[],provenance=dict(purpose='formal',session_id=evidence['session_id'],
            browser_session_id=evidence['browser_session_id'],step_id='S2',
            evidence_id=m['evidence_id'],observation_ref=m['observation_ref'],screenshot_ref=m['screenshot_ref']))
    if method == 'datepicker-instance':
        epoch=int(datetime(2026,11,day,tzinfo=timezone.utc).timestamp()*1000)
        snapshot['component_dates']=[epoch]
        snapshot['selected'][0].update(state_basis='component-dates-and-active-day',
                                      data_date=str(epoch),**{'class':'active day'})
    m['calendar_evidence']=snapshot
    return evidence


def verdict(evidence, expected='2026-11-10'):
    return Agent5EvidenceVerifier().verify_dynamic(plan(expected=expected),evidence)


@pytest.mark.parametrize('method',['aria-controls','datepicker-instance'])
@pytest.mark.parametrize('day,raw,status',[(10,'11/10/2026','PASS'),(11,'11/11/2026','FAIL'),
    (10,'10/11/2026','PASS'),(12,'11/10/2026','INCONCLUSIVE')])
def test_linked_calendar_disambiguates_without_using_expected_to_parse(method,day,raw,status):
    evidence=calendar_evidence(day,raw,method)
    before=deepcopy(evidence)
    result=verdict(evidence)
    assert result.status==status
    assert result.checks[0]['date_comparison']['calendar_actual']==f'2026-11-{day:02}'
    assert result.checks[0]['date_comparison']['calendar_evidence']==evidence['measurements']['O1']['calendar_evidence']
    assert evidence==before
    if status=='PASS':
        different=verdict(evidence,expected='2026-10-11')
        assert different.status=='FAIL'
        assert different.checks[0]['date_comparison']['normalized_actual']=='2026-11-10'


@pytest.mark.parametrize('fault',['screenshot-only','month-only','multiple-calendars','multiple-selected',
    'today-only','hover-only','adjacent','disabled','hidden','closed','wrong-input','multiple-inputs',
    'raw-value-conflict','no-association','no-capture','wrong-capture','trial-capture',
    'wrong-session','wrong-browser','wrong-step','wrong-screenshot','wrong-observation',
    'no-metadata','unsupported-heading','unselected-aria','wrong-role'])
def test_incomplete_conflicting_and_foreign_calendar_evidence_stays_unknown(fault):
    evidence=calendar_evidence()
    m=evidence['measurements']['O1']; c=m['calendar_evidence']; cell=c['selected'][0]
    if fault=='screenshot-only': m.pop('calendar_evidence')
    elif fault=='month-only': c.update(selected=[],selected_count=0)
    elif fault=='multiple-calendars': c['root']['candidate_count']=2
    elif fault=='multiple-selected': c.update(selected=c['selected']*2,selected_count=2)
    elif fault in {'today-only','hover-only'}:
        cell.update(aria_selected='false',aria_current='date' if fault=='today-only' else '',
                    **{'class':'today' if fault=='today-only' else 'focused'})
    elif fault=='adjacent': cell['adjacent']=True
    elif fault=='disabled': cell['disabled']=True
    elif fault=='hidden': cell['visible']=False
    elif fault=='closed': c['root']['visible']=False
    elif fault=='wrong-input': c['association']['input_ref']='/another/input'
    elif fault=='multiple-inputs': c['association']['owner_count']=2
    elif fault=='raw-value-conflict': c['input_value']='11/11/2026'
    elif fault=='no-association': c['association']=None
    elif fault=='no-capture': c.pop('provenance')
    elif fault=='wrong-capture': c['provenance']['evidence_id']='different'
    elif fault=='trial-capture': c['provenance']['purpose']='trial'
    elif fault=='wrong-session': c['provenance']['session_id']='trial'
    elif fault=='wrong-browser': c['provenance']['browser_session_id']='different'
    elif fault=='wrong-step': c['provenance']['step_id']='S1'
    elif fault=='wrong-screenshot': c['provenance']['screenshot_ref']='another.png'
    elif fault=='wrong-observation': c['provenance']['observation_ref']='another.json'
    elif fault=='no-metadata': c.pop('version')
    elif fault=='unsupported-heading': c['heading']['text']='11/2026'
    elif fault=='unselected-aria': cell['aria_selected']='false'
    elif fault=='wrong-role': cell['role']='button'
    assert verdict(evidence).status=='INCONCLUSIVE'


@pytest.mark.parametrize('fault',['active-alone','today-alone','state-disagrees','cell-date-disagrees','two-dates'])
def test_component_active_class_requires_selected_date_state(fault):
    evidence=calendar_evidence(method='datepicker-instance')
    c=evidence['measurements']['O1']['calendar_evidence']
    if fault=='active-alone': c['component_dates']=[]
    elif fault=='today-alone': c['selected'][0]['class']='today day'
    elif fault=='state-disagrees': c['component_dates'][0]+=86400000
    elif fault=='cell-date-disagrees': c['selected'][0]['data_date']='0'
    elif fault=='two-dates': c['component_dates']*=2
    assert verdict(evidence).status=='INCONCLUSIVE'


def test_known_format_has_priority_but_reliable_conflict_cannot_be_hidden():
    evidence=calendar_evidence()
    m=evidence['measurements']['O1']
    m['date_context']['format_attributes']['data-date-format']='MM/DD/YYYY'
    assert verdict(evidence).status=='PASS'
    m['date_context']['format_attributes']['data-date-format']='DD/MM/YYYY'
    assert verdict(evidence).status=='INCONCLUSIVE'
    m.pop('calendar_evidence')
    assert verdict(evidence).status=='FAIL'  # Known DMY alone still compares strictly.


def test_chinese_heading_and_valid_selected_today_are_supported():
    evidence=calendar_evidence()
    c=evidence['measurements']['O1']['calendar_evidence']
    c['heading']['text']='2026年11月'
    c['selected'][0].update(aria_current='date',**{'class':'today'})
    assert verdict(evidence).status=='PASS'  # aria-selected independently proves selection.


def test_trial_cannot_be_substituted_and_execution_error_remains_error():
    evidence=calendar_evidence()
    evidence['purpose']='trial'
    assert verdict(evidence).status=='INCONCLUSIVE'
    evidence['execution_status']='ERROR'
    assert verdict(evidence).status=='ERROR'


def test_model_cannot_supply_calendar_or_provenance_and_dom_retains_real_aria():
    binding=dict(selector='#d',read='attribute',attribute='value',step_id='S2')
    for field in ('calendar_evidence','evidence_id','provenance'):
        with pytest.raises(ValueError,match='unsupported fields'):
            normalize_bindings({'O1':dict(binding,**{field:{}})})
    dom=compact_dom('<input id="d" aria-controls="cal"><div role="grid" id="cal" '
        'aria-labelledby="month"><h2 id="month">November 2026</h2>'
        '<div role="gridcell" aria-selected="true" aria-current="date">10</div></div>')
    assert 'aria-controls="cal"' in dom['html'] and 'aria-selected="true"' in dom['calendar_regions'][0]


def test_ordinary_text_is_not_normalized_by_calendar_fallback():
    contract=plan(); contract['oracles'][0].update(kind='equals',expected='Accepted!')
    evidence=calendar_evidence(raw='Received!')
    assert Agent5EvidenceVerifier().verify_dynamic(contract,evidence).status=='FAIL'
