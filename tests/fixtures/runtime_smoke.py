"""Human-authored, reviewed Selenium acceptance programs; never Agent3 output.

Changing this file requires review and updating the runtime's pinned hash.
These programs have ordinary host permissions and only use synthetic Fixture data.
"""
import time
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


def navigate(ctx):
    driver = ctx.driver
    driver.get(ctx.base_url)
    driver.find_element(By.ID, 'next').click()
    WebDriverWait(driver, 5).until(EC.visibility_of_element_located((By.ID, 'name')))
    print('Navigated to form in browser session', driver.session_id)
    return {'url': driver.current_url, 'session_id': driver.session_id}


def update(ctx):
    driver = ctx.driver
    driver.find_element(By.ID, 'name').send_keys('Synthetic QA')
    driver.find_element(By.ID, 'load').click()
    WebDriverWait(driver, 5).until(EC.element_to_be_clickable((By.ID, 'continue')))
    updated = ctx.capture(region_css='#task')
    assert any(e['id'] == 'continue' and e['visible'] and e['enabled'] for e in updated['live_elements'])
    driver.find_element(By.ID, 'continue').click()
    WebDriverWait(driver, 5).until(EC.text_to_be_present_in_element((By.ID, 'status'), 'Continued'))
    driver.find_element(By.ID, 'menu').click()
    WebDriverWait(driver, 5).until(EC.visibility_of_element_located((By.ID, 'dialog')))
    driver.find_element(By.ID, 'submit').click()
    WebDriverWait(driver, 5).until(EC.text_to_be_present_in_element((By.ID, 'receipt'), '1'))
    print('AJAX control clicked; submitted Synthetic QA; receipt = 1')
    return {'receipt': driver.find_element(By.ID, 'receipt').text,
            'dynamic_control': driver.find_element(By.ID, 'continue').text,
            'status': driver.find_element(By.ID, 'status').text,
            'dialog_visible': driver.find_element(By.ID, 'dialog').is_displayed(),
            'session_id': driver.session_id, 'ajax_observation_ref': updated['observation_ref']}


def smoke(ctx):
    navigate(ctx)
    driver = ctx.driver
    clean = not driver.get_cookie('phase1') and driver.execute_script('return localStorage.getItem("phase1")') is None
    assert clean and ctx.state['submissions'] == []
    before = ctx.capture(region_css='#task')
    details = update(ctx)
    after = ctx.capture(region_css='#task')
    assert before['url'] == after['url'] and before['state_hash'] != after['state_hash']
    driver.add_cookie({'name': 'phase1', 'value': 'synthetic'})
    driver.execute_script('localStorage.setItem("phase1", "synthetic")')
    driver.save_screenshot(str(ctx.run_dir / 'result.png'))
    details.update(initial_storage_clean=clean, initial_submissions=0,
                   before_ref=before['observation_ref'], after_ref=after['observation_ref'])
    return details


def locator_failure(ctx):
    ctx.driver.find_element(By.ID, 'intentionally-missing').click()


def wait_timeout(ctx):
    WebDriverWait(ctx.driver, .4).until(EC.visibility_of_element_located((By.ID, 'never-appears')))


def browser_exception(ctx):
    ctx.driver.close()
    return {'unexpected': ctx.driver.title}


def deadline(ctx):
    print('Trusted deadline probe started', flush=True)
    time.sleep(30)
