"""Business outcome checks; live A/B acceptance is recorded separately in artifacts."""
import pytest

from web_agent.agents.generator import validate_business_waits, Agent3SeleniumGenerator
from web_agent.agents.verifier import Agent5EvidenceVerifier

EXPECTED = 'Specified response!'
CONTRACT = {'oracles':[dict(id='O1',step_id='S1',kind='equals',expected=EXPECTED,
    meaning='response from test basis',source_type='test_basis',source_quote=EXPECTED,
    source_span=dict(start=0,end=len(EXPECTED),line=1))]}


@pytest.mark.parametrize('prefix,text', [('',repr(EXPECTED)), (f'wanted={EXPECTED!r}\n','wanted')])
def test_expected_business_text_cannot_become_a_technical_wait(prefix,text):
    code = prefix + 'WebDriverWait(driver, 10).until(EC.text_to_be_present_in_element((By.ID,"feedback"),'+text+'))'
    with pytest.raises(ValueError,match='Oracle O1.*must not be a Selenium wait'):
        validate_business_waits(code,CONTRACT)


def test_neutral_wait_and_actual_value_binding_are_supported():
    source = 'WebDriverWait(driver,10).until(EC.visibility_of_element_located((By.ID,"feedback")))'
    class Model:
        def complete_json(self,system,user):
            assert 'INDEPENDENTLY' in system and 'intentionally wrong supplied specification' in system
            return dict(code=source,bindings={'O1':dict(by='id',selector='feedback',read='text',step_id='S1')})
    result=Agent3SeleniumGenerator().develop_step(CONTRACT,dict(id='S1'),{},llm=Model())
    assert result['code']==source


@pytest.mark.parametrize('execution,measurement_status,value,status', [
    ('COMPLETE','observed',EXPECTED,'PASS'),
    ('COMPLETE','observed','Actual different response!','FAIL'),
    ('COMPLETE','missing',None,'INCONCLUSIVE'),
    ('COMPLETE','ambiguous',None,'INCONCLUSIVE'),
    ('ERROR','observed',EXPECTED,'ERROR'),
])
def test_formal_actual_values_and_sources_are_preserved(execution,measurement_status,value,status):
    evidence=dict(execution_status=execution,measurements={'O1':dict(read='text',step_id='S1',
        status=measurement_status,value=value,observation_ref='formal.json',screenshot_ref='formal.png')})
    verdict=Agent5EvidenceVerifier().verify_dynamic(CONTRACT,evidence)
    assert verdict.status==status
    assert CONTRACT['oracles'][0]['expected']==EXPECTED
    if execution=='COMPLETE':
        check=verdict.checks[0]
        assert check['actual']==value and check['expected']==EXPECTED
        assert check['source_type']=='test_basis' and check['source_quote']==EXPECTED
