"""Bounded, asynchronous presentation of existing Pipeline events (no model streaming)."""
import json
import queue
import re
import sys
import threading
import unicodedata

ROLES = dict(Agent1='Planner', Agent2='Reviewer', Agent3='Generator', Agent4='Executor',
             Agent5='Validator', Agent6='Diagnoser', Runtime='Runtime', Cold='Cold', Pipeline='RESULT', LLM='LLM')


def safe_display(value, secrets=(), limit=6000):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    text = text[:20000]
    for secret in secrets:
        if secret: text = text.replace(secret, '[REDACTED]')
    text = re.sub(r'(?i)\b(?:sk|sk-or-v1)-[\w-]+', '[REDACTED]', text)
    text = re.sub(r'(https?://)[^/\s@]+@',r'\1[REDACTED]@',text)
    text = re.sub(r'(?im)^.*(?:authorization|api[_ -]?key|password|passwd|secret|access[_ -]?token|cookie).*$','[sensitive line omitted]',text)
    text = re.sub(r'(https?://[^\s\"\'?#]+)[?#][^\s\"\']*', r'\1?[redacted]', text)
    # Remove ANSI/OSC introducers, C1 and bidi formatting, including terminal injection.
    text = ''.join(c for c in text if c in '\n\t' or unicodedata.category(c) not in {'Cc','Cf','Cs'})
    return text[:limit] + ('\n… [truncated]' if len(text) > limit else '')


def format_event(record, secrets=()):
    agent, action = record.get('agent',''), record.get('action','')
    if record.get('phase') == 'mandatory_cold_trial' and agent == 'Runtime': return ''
    seconds = max(0, float(record.get('elapsed_seconds',0)))
    prefix = f'[{int(seconds)//60:02}:{int(seconds)%60:02}] [{ROLES.get(agent, agent):9}] '
    labels = {'run_started':'DEMO MODE · task started', 'semantic_plan_created':'Plan generated',
        'review_finished':'Review approved' if record.get('approved') else 'Review rejected · bounded revision',
        'blockage_reviewed':'Blockage review approved' if record.get('approved') else 'Blockage review rejected',
        'generate_step':'Generating Selenium code', 'code_generated':'Actual Selenium code generated',
        'step_started':'Executing step', 'step_finished':'Execution and evidence capture finished',
        'browser_action':'Browser action', 'session_created':'Chrome session ready',
        'session_starting':'Starting Chrome', 'session_closed':'Chrome session closed',
        'review_feedback':'Revision feedback', 'repair_from_actual_error':'Technical repair',
        'contract_frozen':'Contract frozen', 'source_frozen':'Source frozen',
        'check_finished':'Formal Oracle check', 'business_verdict':'Formal verdict',
        'run_finished':'Final result', 'started':'Started', 'finished':'Finished',
        'call_started':'Model request started', 'call_finished':'Model returned',
        'diagnosed_stop':'Technical diagnosis · stopped', 'layout_warning':'Layout warning',
        'step_complete_observed':'Step completed'}
    # Only explicitly public event fields; never serialize a raw SDK request/response.
    parts = [labels.get(action,action)]
    for key in ('step_id','goal','purpose','reason','status','error_type','attempt','repairs','headless',
                'oracle_id','kind','expected','actual','passed','source_type','issues','warning',
                'operation','action_status','screenshot_ref','observation_ref','current_url'):
        if key in record: parts.append(f'{key}={safe_display(record[key],secrets,800)}')
    if action == 'semantic_plan_created':
        for step in record.get('plan_steps',[])[:40]:
            parts.append(f"  {step.get('id')}: {safe_display(step.get('goal',''),secrets,350)}")
        for oracle in record.get('plan_oracles',[])[:40]:
            parts.append('  Oracle '+safe_display(oracle,secrets,500))
    if action == 'code_generated':
        code = safe_display(record.get('code',''),secrets,5000).splitlines()
        parts.extend('    '+line[:200] for line in code[:24])
        if len(code)>24: parts.append('    … [code limited to 24 lines]')
    return safe_display(prefix+'\n'.join(parts),secrets,9000)


class DemoConsole:
    """FIFO presentation observer. A slow terminal never blocks the test thread."""
    def __init__(self, *, secrets=(), stream=None, capacity=512):
        # TaskStore runs in the Streamlit server process: stdout is its launching
        # terminal, including VS Code's integrated terminal. No console is spawned.
        self.secrets, self.stream = tuple(secrets), sys.stdout if stream is None else stream
        self.queue = queue.Queue(maxsize=capacity)
        self.closed = False
        self.dropped = 0
        self.thread = threading.Thread(target=self._write, daemon=True, name='webagent-demo-console')
        self.thread.start()

    def __call__(self, record):
        if self.closed: return
        text = format_event(record, self.secrets)
        if not text: return
        try: self.queue.put_nowait(text)
        except queue.Full: self.dropped += 1

    def _write(self):
        reported_drops = 0
        while True:
            try: text = self.queue.get(timeout=.1)
            except queue.Empty:
                if self.closed: return
                continue
            try:
                if self.dropped != reported_drops:
                    self.stream.write(f'[Runtime] Presentation queue full: {self.dropped} display entries omitted; original events retained.\n')
                    reported_drops = self.dropped
                self.stream.write(text+'\n'); self.stream.flush()
            except Exception:
                pass  # Presentation cannot alter execution or trigger code repair.
            finally: self.queue.task_done()

    def close(self):
        self.closed = True
        self.thread.join(timeout=.3)
        # Do not retain the task credential after the observer has finished.
        self.secrets = ()
