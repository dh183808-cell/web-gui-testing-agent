"""Offline strategy execution and prompt contracts. No real model/Chrome claims."""
from copy import deepcopy
import json
import pytest
from selenium.common.exceptions import NoSuchElementException, TimeoutException
from selenium.webdriver.common.by import By

from tests.date_navigation_support import navigation_code, selection_code, title
from tests.test_date_semantics import plan
from web_agent.agents.generator import Agent3SeleniumGenerator, validate_business_waits
from web_agent.agents.reviewer import Agent2TestReviewer
from web_agent.checked_selenium import execute_source, validate_source


class CalendarDriver:
    """Small observable calendar for the REAL checked interpreter/EC execution."""
    current_url = 'http://127.0.0.1/calendar'
    current_window_handle = 'local'
    window_handles = ['local']
    title = 'Offline calendar'

    def __init__(self, start, *, fault=None):
        self.year, self.month = start
        self.fault, self.directions, self.selected = fault, [], False
        self.heading_reads, self.reads_at_click = 0, []

    def execute_script(self, *args):
        return dict(href='', target='')  # Trusted click metadata, never generated JS.

    def find_element(self, by, locator):
        if by == By.XPATH:
            if f'="{title(self.year, self.month)}"' not in locator:
                raise NoSuchElementException('Target year/month heading not reached')
            locator = 'month'
        return self.Node(self, locator)

    class Node:
        def __init__(self, driver, kind):
            self.driver, self.kind = driver, kind

        def get_attribute(self, key):
            return self.kind if key == 'id' else ''

        def is_displayed(self):
            return True

        def is_enabled(self):
            return True

        @property
        def text(self):
            self.driver.heading_reads += 1
            if self.driver.fault == 'unparseable':
                return 'Unknown calendar era'
            if self.driver.fault == 'prefix-conflict' and self.driver.directions:
                return title(self.driver.year, self.driver.month) + '0'
            return title(self.driver.year, self.driver.month)

        def click(self):
            d = self.driver
            if self.kind == 'day10':
                d.selected = True
                return
            direction = 1 if self.kind == 'next' else -1
            d.directions.append(direction)
            d.reads_at_click.append(d.heading_reads)
            if d.fault == 'stuck':
                return
            if d.fault == 'wrong-direction':
                direction = -direction
            index = d.year * 12 + d.month - 1 + direction
            d.year, zero_month = divmod(index, 12)
            d.month = zero_month + 1


@pytest.mark.parametrize('start,target,directions', [
    ((2026,11), (2026,11), []),
    ((2026,10), (2026,11), [1]),
    ((2026,12), (2026,11), [-1]),
    ((2025,12), (2026,11), [1]*11),
    ((2027,1), (2026,11), [-1]*2),
    ((2025,12), (2026,1), [1]),
    ((2026,1), (2025,12), [-1]),
])
def test_checked_navigation_direction_transition_and_stop(start, target, directions):
    driver = CalendarDriver(start)
    source = navigation_code(target, timeout=.1)
    validate_source(source)  # Existing syntax/node/operation limits are enforced.
    execute_source(source, driver, driver.current_url)
    execute_source(selection_code(target, timeout=.1), driver, driver.current_url)
    assert (driver.year, driver.month) == target
    assert driver.directions == directions and driver.selected
    assert all(after > before for before, after in zip(driver.reads_at_click, driver.reads_at_click[1:]))


@pytest.mark.parametrize('fault,error,clicks', [
    ('unparseable', IndexError, 0), ('stuck', TimeoutException, 1),
    ('wrong-direction', TimeoutException, 1), ('prefix-conflict', IndexError, 1),
])
def test_invalid_heading_or_transition_fails_before_further_click(fault, error, clicks):
    driver = CalendarDriver((2026,10), fault=fault)
    with pytest.raises(error):
        execute_source(navigation_code(timeout=.1), driver, driver.current_url)
        execute_source(selection_code(timeout=.1), driver, driver.current_url)
    assert len(driver.directions) == clicks and not driver.selected


def test_navigation_exhaustion_cannot_fall_through_to_selection():
    driver = CalendarDriver((2025,12))
    with pytest.raises(TimeoutException):
        execute_source(navigation_code(limit=3, timeout=.1), driver, driver.current_url)
        execute_source(selection_code(timeout=.1), driver, driver.current_url)
    assert driver.directions == [1]*3 and not driver.selected


def test_target_reached_on_last_allowed_click_is_valid():
    driver = CalendarDriver((2025,12))
    execute_source(navigation_code(limit=11, timeout=.1), driver, driver.current_url)
    assert driver.directions == [1]*11 and (driver.year, driver.month) == (2026,11)


def test_selection_rechecks_year_after_navigation_or_reopening():
    driver = CalendarDriver((2026,11))
    execute_source(navigation_code(timeout=.1), driver, driver.current_url)
    driver.year = 2025  # Same month, changed page state between separate steps.
    with pytest.raises(TimeoutException):
        execute_source(selection_code(timeout=.1), driver, driver.current_url)
    assert not driver.selected


def test_strategy_guard_is_technical_and_final_business_value_is_still_measured():
    validate_business_waits(navigation_code(), plan())
    with pytest.raises(ValueError, match='business expected text'):
        validate_business_waits('WebDriverWait(driver,1).until(EC.text_to_be_present_in_element((By.ID,"date"),"2026-11-10"))', plan())


def test_generator_prompt_constraints_are_executable_without_new_capabilities():
    class Model:
        def complete_json(self, system, user):
            for requirement in ('FULL (year, month)', 'After EACH click', 'expected FULL next year/month',
                'exact target heading', 'DATE-SELECTION step', 'arithmetic/int/split/break/assert/raise',
                'Keep the original bound', "final input's business date equality"):
                assert requirement in system
            return dict(code=navigation_code(), bindings={})
    result = Agent3SeleniumGenerator().develop_step(plan(), {'id':'S1'}, {}, llm=Model())
    driver = CalendarDriver((2025,12))
    execute_source(result['code'], driver, driver.current_url)
    assert driver.directions == [1]*11


def test_reviewer_prompt_and_rejection_feedback_for_month_first_code():
    # Scripted review response tests prompt/input/feedback plumbing only. It does
    # NOT claim that a real LLM has understood or rejected this code.
    bad_code = '''month = 12
year = 2025
if month > 11:
    driver.find_element(By.ID, "prev").click()
if month == 11:
    if year < 2026:
        driver.find_element(By.ID, "next").click()'''
    contract = plan()
    original = deepcopy(contract)
    class ScriptedReview:
        def complete_json(self, system, user):
            data = json.loads(user)
            assert data['implementation'][0]['source'] == bad_code
            for requirement in ('Reject month-first', 'after EACH click', 'unparseable/out-of-range',
                'exhausted iterations must fail', 'immediately before its click', 'ignored errors',
                'repair_scope="code"', 'existing bounded revision mechanism'):
                assert requirement in system
            return dict(approved=False, issue_type='implementation', repair_scope='code', step_ids=['S1'],
                issues=['Month-first comparison navigates backwards across years. Compare year then month; '
                    'wait and re-read each exact transition; guard the target after the loop and before selecting.'])
    result = Agent2TestReviewer().review_implementation(contract, [dict(id='S1', source=bad_code)],
        {'O1':dict(read='attribute', attribute='value')}, observation={}, llm=ScriptedReview())
    assert not result['approved'] and result['repair_scope'] == 'code'
    assert result['step_ids'] == ['S1'] and 'year then month' in result['issues'][0]
    assert contract == original
