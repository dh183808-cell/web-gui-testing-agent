"""Shared Selenium runtime: managed LOCAL trusted sessions and v0.3 actions.

Local subprocesses have host permissions. Default mode only runs reviewed fixture
programs; explicit research mode interprets the checked Selenium capability subset.
"""
from __future__ import annotations

import atexit
from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import time
import traceback
import re
import uuid
from urllib.parse import parse_qs, urlparse

from .evidence_plan import collect_checkpoint, normalize_id

ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_ROOT = ROOT / 'artifacts'
TRUSTED_SCRIPT_IDS = frozenset({'navigate', 'update', 'smoke', 'locator_failure',
                              'wait_timeout', 'browser_exception', 'deadline'})
# Review gate for the human-authored fixture asset; never calculate approval at runtime.
REVIEWED_SCRIPT_SHA256 = '68da6297c3c8827b505ec53e99a9654567bc4a2720df52eb8afc89c77a25d5ce'
CHECKED_PHASES = ('pre_observe', 'script_execute', 'oracle_capture', 'post_observe')


def validate_presentation_delay(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 3:
        raise ValueError('正式执行页面停留时间须为 0～3 秒')
    return float(value)


def phase_domain(phase):
    return 'observation' if phase in {'pre_observe', 'post_observe'} else 'oracle' if phase == 'oracle_capture' else 'script'


def reviewed_script():
    path = ROOT / 'tests' / 'fixtures' / 'runtime_smoke.py'
    if hashlib.sha256(path.read_bytes().replace(b'\r\n', b'\n')).hexdigest() != REVIEWED_SCRIPT_SHA256:
        raise PermissionError('Trusted fixture source changed; human review and hash update required')
    return path, REVIEWED_SCRIPT_SHA256


@dataclass(frozen=True)
class RuntimeLimits:
    """Wall-clock limits only. No CPU/memory/file/network sandbox is claimed."""
    execution_seconds: float = 30
    startup_seconds: float = 60
    session_seconds: float = 600
    step_attempts: int = 3
    task_repairs: int = 5
    observation_seconds: float = 5
    oracle_seconds: float = 5

    def __post_init__(self):
        for name, low, high in [('execution_seconds', .1, 300), ('observation_seconds', .1, 30),
                                ('oracle_seconds', .1, 30), ('startup_seconds', 1, 180),
                                ('session_seconds', 1, 1800), ('step_attempts', 1, 3), ('task_repairs', 0, 5)]:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f'Invalid runtime limit: {name}')
        for name in ('step_attempts', 'task_repairs'):
            if not isinstance(getattr(self, name), int):
                raise ValueError(f'{name} must be an integer')

    def checked_phase_limits(self):
        return dict(pre_observe=self.observation_seconds, script_execute=self.execution_seconds,
                    oracle_capture=self.oracle_seconds, post_observe=self.observation_seconds)


class RuntimePhaseTimeout(TimeoutError):
    def __init__(self, phase, partial, elapsed, scope='phase'):
        super().__init__(f'{phase} exceeded its {scope} deadline')
        self.partial = dict(partial)
        self.partial.update(failed_phase=phase, failure_domain=phase_domain(phase), timeout_scope=scope)
        self.partial['timings'] = dict(partial.get('timings',{}), **{phase:round(elapsed,3)})
        self.partial['phase_status'] = dict(partial.get('phase_status',{}), **{phase:'TIMEOUT'})
        if phase == 'script_execute':
            self.partial['script_status'] = 'TIMEOUT'
        elif phase == 'oracle_capture':
            self.partial['oracle_status'] = 'TIMEOUT'
            log = partial.get('oracle_log', [])
            if log and log[-1].get('event') == 'started':
                active = dict(log[-1],event='timeout',
                    elapsed_seconds=round(max(0,elapsed-log[-1].get('started_seconds',0)),4))
                self.partial.update(oracle_timeout=active,oracle_log=[*log,active])


class LocalRuntimeUnavailable(RuntimeError):
    pass


def _worker_environment(temp_dir):
    """Reduce accidental credential inheritance; this is not access isolation."""
    allowed = {'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATH', 'PATHEXT', 'SYSTEMDRIVE'}
    env = {k: v for k, v in os.environ.items() if k.upper() in allowed}
    env.update(TEMP=str(temp_dir), TMP=str(temp_dir), PYTHONIOENCODING='utf-8',
               PYTHONUNBUFFERED='1')
    return env


class _OwnedProcessTree:
    """Windows Job lifecycle control, NOT a security sandbox."""
    def __init__(self):
        if os.name != 'nt':
            raise LocalRuntimeUnavailable('Managed local sessions currently require Windows Job Objects')
        import ctypes
        from ctypes import wintypes as w
        self.c = ctypes
        self.k = ctypes.WinDLL('kernel32', use_last_error=True)
        self.k.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
        self.k.CreateJobObjectW.restype = w.HANDLE
        self.k.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
        self.k.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        self.k.TerminateJobObject.argtypes = [w.HANDLE, w.UINT]
        self.k.QueryInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.c_void_p]
        self.k.CloseHandle.argtypes = [w.HANDLE]
        class Basic(ctypes.Structure):
            _fields_ = [('PerProcessUserTimeLimit', ctypes.c_int64), ('PerJobUserTimeLimit', ctypes.c_int64),
                        ('LimitFlags', w.DWORD), ('MinimumWorkingSetSize', ctypes.c_size_t),
                        ('MaximumWorkingSetSize', ctypes.c_size_t), ('ActiveProcessLimit', w.DWORD),
                        ('Affinity', ctypes.c_size_t), ('PriorityClass', w.DWORD), ('SchedulingClass', w.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in ('ReadOperationCount', 'WriteOperationCount',
                          'OtherOperationCount', 'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]
        class Extended(ctypes.Structure):
            _fields_ = [('BasicLimitInformation', Basic), ('IoInfo', IO),
                        ('ProcessMemoryLimit', ctypes.c_size_t), ('JobMemoryLimit', ctypes.c_size_t),
                        ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t)]
        self.handle = self.k.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = Extended()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE; no breakaway
        if not self.k.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def assign(self, process):
        if not self.k.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise self.c.WinError(self.c.get_last_error())

    def active(self):
        c = self.c
        from ctypes import wintypes as w
        class Accounting(c.Structure):
            _fields_ = [(n, c.c_int64) for n in ('TotalUserTime', 'TotalKernelTime', 'ThisPeriodTotalUserTime', 'ThisPeriodTotalKernelTime')] + [
                (n, w.DWORD) for n in ('TotalPageFaultCount', 'TotalProcesses', 'ActiveProcesses', 'TotalTerminatedProcesses')]
        info = Accounting()
        if not self.k.QueryInformationJobObject(self.handle, 1, c.byref(info), c.sizeof(info), None):
            raise c.WinError(c.get_last_error())
        return info.ActiveProcesses

    def terminate(self):
        if not self.k.TerminateJobObject(self.handle, 1):
            raise self.c.WinError(self.c.get_last_error())

    def close(self):
        if self.handle:
            self.k.CloseHandle(self.handle)
            self.handle = None


class LocalSession:
    """Control handle; a trusted Worker exclusively owns one persistent WebDriver.

    No WebDriver object crosses the process boundary. Worker commands are limited
    to observations, registered fixture functions and close. A fresh session also
    creates a private Fixture server and data store, not just a clean Profile.
    """
    def __init__(self, run_dir: Path, *, purpose='trial', seed='phase1', limits=None,
                 headless=True, driver_path=None, chrome_binary=None, start_url=None, research_mode=False,
                 formal_demo_delay=0.0):
        if purpose not in {'trial', 'development', 'formal'}:
            raise ValueError('Invalid execution purpose')
        if not isinstance(seed, str) or len(seed) > 100:
            raise ValueError('Invalid synthetic seed')
        root = Path(run_dir).resolve()
        if not root.is_relative_to(ARTIFACT_ROOT.resolve()):
            raise ValueError('Run directory must be inside artifacts/')
        if start_url:
            from .checked_selenium import origin
            origin(start_url)
            if not research_mode:
                raise ValueError('External sessions require explicit research_mode')
        reviewed_script()
        selected_driver = driver_path or os.getenv('CHROMEDRIVER_PATH') or shutil.which('chromedriver')
        if not selected_driver or not Path(selected_driver).is_file():
            raise LocalRuntimeUnavailable('Provide an installed ChromeDriver using --driver-path or CHROMEDRIVER_PATH; automatic downloads are disabled')
        driver_path = Path(selected_driver).resolve()
        self.limits = limits or RuntimeLimits()
        self.session_id = uuid.uuid4().hex
        self.purpose, self.seed = purpose, seed
        delay = validate_presentation_delay(formal_demo_delay)
        self.formal_demo_delay = delay if purpose == 'formal' else 0.0
        self._pause_cancel = threading.Event()
        self.run_dir = root / purpose / self.session_id
        self.run_dir.mkdir(parents=True, exist_ok=False)
        self.research_mode = research_mode
        self.progress_callback = None
        self._options = dict(headless=headless, driver_path=driver_path, chrome_binary=chrome_binary,
                             start_url=start_url, research_mode=research_mode, formal_demo_delay=self.formal_demo_delay)
        self._root = root
        self._responses = queue.Queue()
        self._lock = threading.RLock()
        self._closed, self._proc, self._job, self._timer, self._reader = False, None, None, None, None
        self._last_url = self._last_observation_ref = None
        self._expires = None
        self.cleanup = {'process_tree_terminated': False, 'owned_processes_remaining': None,
                        'profile_removed': False, 'driver_quit': False}
        self.metadata = dict(session_id=self.session_id, purpose=purpose, seed=seed,
                             headless=headless,
                             formal_demo_delay=self.formal_demo_delay,
                             limits=asdict(self.limits), status='STARTING', execution_mode='local_trusted',
                             security_sandbox=False)
        self._exit_handler = self.close_session
        self._stderr = None
        try:
            self._job = _OwnedProcessTree()
            temp = self.run_dir / 'tmp'
            temp.mkdir()
            self._stderr = (self.run_dir / 'worker_stderr.log').open('w', encoding='utf-8')
            entry = f"import sys;sys.path.insert(0,{str(ROOT)!r});from web_agent.local_worker import main;main()"
            self._proc = subprocess.Popen([sys.executable, '-I', '-c', entry], cwd=self.run_dir,
                                          env=_worker_environment(temp), stdin=subprocess.PIPE,
                                          stdout=subprocess.PIPE, stderr=self._stderr, text=True, encoding='utf-8',
                                          creationflags=subprocess.CREATE_NO_WINDOW)
            # Worker waits for init and cannot start Chrome before assignment.
            self._job.assign(self._proc)
            self._reader = threading.Thread(target=self._read_responses, daemon=True)
            self._reader.start()
            config = dict(run_dir=str(self.run_dir), seed=seed, headless=headless,
                          start_url=start_url, research_mode=research_mode,
                          driver_path=str(driver_path or os.getenv('CHROMEDRIVER_PATH') or '') or None,
                          chrome_binary=str(chrome_binary or os.getenv('CHROME_BINARY') or '') or None)
            response = self._request(config, self.limits.startup_seconds)
            if response.get('status') != 'READY':
                raise LocalRuntimeUnavailable(response.get('error_type', 'Browser startup failed') + ': ' + response.get('error', ''))
            self.metadata.update(response, status='READY', worker_pid=self._proc.pid,
                                 owned_processes_at_start=self._job.active())
            self._last_url = response['base_url']
            self._expires = time.monotonic() + self.limits.session_seconds
            self._write('session.json', self.metadata)
            atexit.register(self._exit_handler)
            self._timer = threading.Timer(self.limits.session_seconds, self._expire)
            self._timer.daemon = True
            self._timer.start()
        except BaseException:
            self.close_session(graceful=False)
            raise

    def _write(self, name, value):
        (self.run_dir / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')

    def _read_responses(self):
        try:
            for line in self._proc.stdout:
                self._responses.put(json.loads(line))
        except (ValueError, OSError):
            pass
        finally:
            self._responses.put({'status': 'ERROR', 'error_type': 'WorkerExited', 'error': 'Worker control stream closed'})

    def _request(self, payload, timeout):
        with self._lock:
            if self._closed or self._proc is None:
                raise RuntimeError('Session is closed')
            if self._expires is not None and payload.get('op') != 'close':
                timeout = min(timeout, self._expires - time.monotonic())
                if timeout <= 0:
                    raise TimeoutError('Managed session deadline exceeded')
            self._proc.stdin.write(json.dumps(payload) + '\n')
            self._proc.stdin.flush()
            phase_limits = payload.get('phase_limits')
            started = time.monotonic()
            request_deadline = started + timeout
            phase, phase_started = 'pre_observe', started
            partial = dict(timings=dict.fromkeys(CHECKED_PHASES,0.0),
                           phase_status=dict.fromkeys(CHECKED_PHASES,'NOT_RUN'),
                           script_status='NOT_RUN',oracle_status='NOT_RUN')
            phase_deadline = started + phase_limits[phase] if phase_limits else request_deadline
            seen = set()
            while True:
                deadline = min(request_deadline, phase_deadline)
                if self._expires is not None and payload.get('op') != 'close':
                    deadline = min(deadline,self._expires)
                remaining = deadline - time.monotonic()
                try:
                    if remaining <= 0:
                        raise queue.Empty()
                    result = self._responses.get(timeout=remaining)
                except queue.Empty:
                    if phase_limits:
                        now = time.monotonic()
                        scope = 'session' if self._expires is not None and now >= self._expires else (
                            'request' if now >= request_deadline else 'phase')
                        raise RuntimePhaseTimeout(phase,partial,now-phase_started,scope) from None
                    raise TimeoutError('Managed execution deadline exceeded') from None
                if not phase_limits or '_runtime_phase' not in result:
                    break
                name = result['_runtime_phase']
                if name not in phase_limits:
                    raise LocalRuntimeUnavailable('Unknown Worker phase')
                if result['event'] == 'progress':
                    if name != phase or name != 'oracle_capture':
                        raise LocalRuntimeUnavailable('Oracle progress outside capture phase')
                    delta = result['partial']
                    partial.setdefault('oracle_log',[]).append(delta['oracle_progress'])
                    partial.setdefault('measurements',{}).update(delta.get('measurements',{}))
                    continue
                if result['event'] == 'started':
                    if name in seen or CHECKED_PHASES.index(name) < CHECKED_PHASES.index(phase):
                        raise LocalRuntimeUnavailable('Invalid Worker phase order')
                    seen.add(name)
                    phase, phase_started = name, time.monotonic()
                    phase_deadline = phase_started + phase_limits[name]
                partial = result['partial']
            if result.get('error_type') == 'WorkerExited':
                raise LocalRuntimeUnavailable(result['error'])
            return result

    def _remember(self, observation):
        self._last_url = observation['url']
        self._last_observation_ref = observation['observation_ref']
        return observation

    def observe(self, *, screenshot=False, **options):
        try:
            result = self._request({'op': 'observe', 'options': options, 'screenshot': screenshot},
                                   self.limits.observation_seconds)
            if result['status'] != 'COMPLETE':
                raise RuntimeError(result.get('error_type', 'Observation failed'))
            return self._remember(result['observation'])
        except Exception:
            self.close_session(graceful=False)
            raise

    def execute_checked(self, source, *, step_id=None, queries=None):
        """Research opt-in: interpret a bounded Selenium subset; never arbitrary Python."""
        if getattr(self, 'research_mode', False):
            self._progress('step_started', step_id=step_id)
            result = self._execute_research(source, step_id=step_id, queries=queries or {})
            self._progress('step_finished', step_id=step_id, status=result['status'],
                           timings=result.get('timings'), elapsed_seconds_step=result.get('elapsed_seconds'))
            return result
        result = dict(status='REJECTED', error_type='UntrustedCodeDisabled',
                      error='Arbitrary Python execution is disabled in local mode; use a reviewed script ID',
                      session_id=self.session_id, purpose=self.purpose, elapsed_seconds=0,
                      current_url=self._last_url, observation_ref=self._last_observation_ref, cleanup=dict(self.cleanup))
        self._write('rejection_' + uuid.uuid4().hex + '.json', result)
        return result

    def _progress(self, action, **data):
        callback = getattr(self, 'progress_callback', None)
        if callback:
            try:
                callback(action, session_id=self.session_id, **data)
            except Exception:
                pass

    def _execute_research(self, source, *, step_id, queries):
        from .checked_selenium import validate_source, normalize_bindings
        started = time.monotonic()
        result = dict(status='REJECTED', step_id=step_id, execution_id=uuid.uuid4().hex,
                      session_id=self.session_id, purpose=self.purpose,
                      current_url=self._last_url, observation_ref=self._last_observation_ref)
        try:
            validate_source(source)
            queries = normalize_bindings(queries, step_id=step_id)
            result['script_sha256'] = hashlib.sha256(source.encode()).hexdigest()
            phase_limits = self.limits.checked_phase_limits()
            response = self._request({'op': 'checked', 'source': source, 'step_id': step_id,
                                      'queries': queries, 'phase_limits':phase_limits}, sum(phase_limits.values()))
            observation = response.pop('observation', None)
            result.update(response)
            if observation:
                self._remember(observation)
                result.update(current_url=self._last_url, observation_ref=self._last_observation_ref)
        except (ValueError, SyntaxError) as exc:
            result.update(status='REJECTED', error_type=type(exc).__name__, error=str(exc)[:1200])
        except Exception as exc:
            if isinstance(exc,RuntimePhaseTimeout):
                result.update(exc.partial)
            result.update(status='TIMEOUT' if isinstance(exc, TimeoutError) else 'ERROR',
                          error_type=type(exc).__name__,
                          error=str(exc) if isinstance(exc,RuntimePhaseTimeout) else 'Research Worker request failed')
            self.close_session(graceful=False)
        result.update(elapsed_seconds=round(time.monotonic()-started, 3), cleanup=dict(self.cleanup))
        self._write('execution_' + result['execution_id'] + '.json', result)
        return result

    def execute_program(self, source, *, step_ids, bindings):
        from .checked_selenium import split_program
        steps = split_program(source)
        if [s['id'] for s in steps] != step_ids:
            raise ValueError('Frozen program does not cover the reviewed steps')
        results = []
        for step in steps:
            queries = {key: query for key, query in bindings.items() if query['step_id'] == step['id']}
            result = self.execute_checked(step['source'], step_id=step['id'], queries=queries)
            results.append(result)
            if result['status'] != 'COMPLETE':
                break
            # Trusted control-side pacing, AFTER evidence and outside Worker phase
            # deadlines. Includes the final page; never changes generated source.
            if getattr(self, 'formal_demo_delay', 0) and self.purpose == 'formal':
                started = time.monotonic()
                self._progress('presentation_pause_started', step_id=step['id'], seconds=self.formal_demo_delay)
                cancelled = self._pause_cancel.wait(self.formal_demo_delay)
                result['presentation'] = dict(requested_seconds=self.formal_demo_delay,
                    elapsed_seconds=round(time.monotonic()-started, 3), cancelled=cancelled)
                self._write('presentation_' + step['id'] + '.json', result['presentation'])
                self._progress('presentation_pause_finished', step_id=step['id'],
                    duration_seconds=result['presentation']['elapsed_seconds'], cancelled=cancelled)
                if cancelled:
                    result.update(status='ERROR', error_type='SessionClosedDuringPresentation')
                    break
        return dict(execution_status='COMPLETE' if len(results) == len(steps) and all(
                        r['status']=='COMPLETE' for r in results) else 'ERROR',
                    script_sha256=hashlib.sha256(source.encode()).hexdigest(),
                    session_id=self.session_id, browser_session_id=self.metadata['browser_session_id'],
                    purpose=self.purpose, steps=results,
                    timings={phase:round(sum(r.get('timings',{}).get(phase,0) for r in results),3)
                             for phase in CHECKED_PHASES},
                    measurements={key: v for r in results for key,v in r.get('measurements',{}).items()})

    def execute_trusted(self, script_id: str):
        started = time.monotonic()
        result = dict(status='REJECTED', script_id=script_id, script_sha256=REVIEWED_SCRIPT_SHA256,
                      execution_id=uuid.uuid4().hex, session_id=self.session_id, purpose=self.purpose,
                      current_url=self._last_url, observation_ref=self._last_observation_ref)
        if script_id not in TRUSTED_SCRIPT_IDS:
            result.update(error_type='UntrustedCodeDisabled', error='Unknown reviewed script ID')
        else:
            try:
                path, digest = reviewed_script()
                (self.run_dir / 'reviewed_script.py').write_bytes(path.read_bytes())
                response = self._request({'op': 'execute', 'script_id': script_id}, self.limits.execution_seconds)
                observation = response.pop('observation', None)
                result.update(response)
                if observation:
                    self._remember(observation)
                    result.update(current_url=self._last_url, observation_ref=self._last_observation_ref)
                if result['status'] != 'COMPLETE':
                    self.close_session()
            except Exception as exc:
                result.update(status='TIMEOUT' if isinstance(exc, TimeoutError) else 'ERROR',
                              error_type=type(exc).__name__, error=str(exc)[:300])
                self.close_session(graceful=False)
        result.update(elapsed_seconds=round(time.monotonic() - started, 3), cleanup=dict(self.cleanup))
        self._write('execution_' + result['execution_id'] + '.json', result)
        return result

    def _expire(self):
        self.metadata['termination_reason'] = 'session_deadline'
        self.close_session(graceful=False)

    def close_session(self, *, graceful=True):
        pause_cancel = getattr(self, '_pause_cancel', None)
        if pause_cancel: pause_cancel.set()
        with self._lock:
            if self._closed:
                return dict(self.cleanup)
            if self._timer:
                self._timer.cancel()
            atexit.unregister(self._exit_handler)
            if graceful and self._proc and self._proc.poll() is None:
                try:
                    self.cleanup['driver_quit'] = self._request({'op': 'close'}, 5).get('driver_quit', False)
                except Exception:
                    pass
            self._closed = True
            if self._job:
                try:
                    self.cleanup['owned_processes_before_termination'] = self._job.active()
                    self._job.terminate()
                    until = time.monotonic() + 5
                    remaining = self._job.active()
                    while remaining and time.monotonic() < until:
                        time.sleep(.05)
                        remaining = self._job.active()
                    self.cleanup.update(owned_processes_remaining=remaining, process_tree_terminated=remaining == 0)
                except OSError as exc:
                    self.cleanup['cleanup_error_type'] = type(exc).__name__
                finally:
                    self._job.close()
            if self._proc:
                if self._proc.poll() is None:
                    self._proc.kill()  # also covers failure before Job assignment; no browser launched yet
                self._proc.wait(timeout=5)
                if self._reader:
                    self._reader.join(timeout=2)
                for stream in (self._proc.stdin, self._proc.stdout):
                    stream.close()
            if self._stderr:
                self._stderr.close()
            if self.cleanup['process_tree_terminated']:
                profile = self.run_dir / 'profile'
                for attempt in range(20):
                    try:
                        if profile.exists():
                            if not profile.resolve().is_relative_to(self.run_dir.resolve()):
                                raise ValueError('Profile cleanup target escaped its session directory')
                            shutil.rmtree(profile)
                        self.cleanup['profile_removed'] = True
                        break
                    except OSError:
                        time.sleep(.1)
            self.metadata.update(status='CLOSED', cleanup=dict(self.cleanup))
            self._write('session.json', self.metadata)
            return dict(self.cleanup)

    def reset_environment(self):
        if not self.close_session()['process_tree_terminated']:
            raise LocalRuntimeUnavailable('Previous owned process tree cleanup not confirmed')
        return create_session(self._root, purpose=self.purpose, seed=self.seed,
                              limits=self.limits, **self._options)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close_session()


def create_session(run_dir: Path, *, purpose='trial', **options) -> LocalSession:
    return LocalSession(run_dir, purpose=purpose, **options)


def make_driver(*, headless: bool = True, profile_dir: Path | None = None,
                proxy_url: str | None = None, driver_path=None, chrome_binary=None, service_log=None,
                page_load_strategy='normal'):
    """Import Selenium lazily so contract/generation tests work without a browser."""
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service

    opts = Options()
    opts.add_argument("--window-size=1440,1000")
    if headless:
        opts.add_argument("--headless=new")
    if os.name == "posix":
        opts.add_argument("--disable-dev-shm-usage")
    if profile_dir is not None:
        opts.add_argument(f"--user-data-dir={profile_dir}")
    if proxy_url:
        opts.add_argument(f"--proxy-server={proxy_url}")
        opts.add_argument("--proxy-bypass-list=<-loopback>")
        opts.add_argument("--disable-quic")
        opts.add_argument("--force-webrtc-ip-handling-policy=disable_non_proxied_udp")
    opts.add_argument("--disable-background-networking")
    opts.add_argument("--disable-extensions")
    opts.add_experimental_option("prefs", {"download_restrictions": 3,
                                         "credentials_enable_service": False,
                                         "profile.password_manager_enabled": False})
    if chrome_binary or os.getenv("CHROME_BINARY"):
        opts.binary_location = str(chrome_binary or os.environ["CHROME_BINARY"])
    opts.page_load_strategy = page_load_strategy
    executable = driver_path or os.getenv('CHROMEDRIVER_PATH')
    service = Service(executable_path=str(executable) if executable else None,
                      log_output=str(service_log) if service_log else None)
    driver = webdriver.Chrome(service=service, options=opts)
    driver.set_page_load_timeout(30)
    return driver


def inspect_page(url: str, *, headless: bool = True) -> dict:
    """Legacy one-shot inspection; this layer owns driver creation and cleanup.

    Dynamic sessions use the existing Worker and observe_session on its driver.
    DOM tools do not import the Runtime or own browser lifetimes.
    """
    from .dom_inspector import observe_session
    driver = make_driver(headless=headless)
    try:
        driver.get(url)
        return observe_session(driver)
    finally:
        driver.quit()


def _first_displayed(driver, css: list[str], *, timeout: int = 12):
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait

    def locate(d):
        for selector in css:
            for node in d.find_elements(By.CSS_SELECTOR, selector):
                try:
                    if node.is_displayed() and node.is_enabled():
                        return node
                except Exception:
                    continue
        return False

    return WebDriverWait(driver, timeout).until(locate)


def _control_description(node) -> dict:
    def read(call, default=""):
        try:
            value = call()
            return str(value or "")[:160]
        except Exception:
            return default
    return {
        "text": read(lambda: node.text),
        "type": read(lambda: node.get_attribute("type")),
        "aria_label": read(lambda: node.get_attribute("aria-label")),
        "value": read(lambda: node.get_attribute("value")),
        "outer_html": read(lambda: node.get_attribute("outerHTML"))[:220],
    }


def _form_submit(driver, keyword_node, candidates: list[str], *, timeout: int = 12):
    """Select a Search submit control *from the keyword field's form*.

    Forms commonly have multiple buttons, and the actual Search button can be
    <button>Search</button> (implicit submit). A page-wide button[type=submit]
    can match the unrelated header search. Never fall back to an unrelated form.
    """
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait

    def locate(d):
        try:
            form = keyword_node.find_element(By.XPATH, "./ancestor::form[1]")
            controls = form.find_elements(By.CSS_SELECTOR, 'button, input[type="submit"]')
        except Exception:
            # Some custom forms associate a control via form=ID rather than ancestry.
            # Only use candidates explicitly constrained to the advanced-search form.
            controls = []
            for selector in candidates:
                if "form" not in selector:
                    continue
                try:
                    controls.extend(d.find_elements(By.CSS_SELECTOR, selector))
                except Exception:
                    continue
        ranked = []
        for node in controls:
            try:
                if not node.is_displayed() or not node.is_enabled():
                    continue
                info = _control_description(node)
                label = (info["text"] or info["aria_label"] or info["value"]).strip().casefold()
                node_type = info["type"].casefold()
                if node_type in {"button", "reset"}:
                    continue
                if label == "search":
                    priority = 0
                elif label.startswith("search ") or label.endswith(" search"):
                    priority = 1
                elif node_type == "submit" and not label:
                    priority = 2
                else:
                    continue
                ranked.append((priority, node))
            except Exception:
                continue
        if not ranked:
            return False
        ranked.sort(key=lambda item: item[0])
        return ranked[0][1]

    return WebDriverWait(driver, timeout).until(locate)


def _has_no_results(driver) -> bool:
    from selenium.webdriver.common.by import By
    try:
        if driver.find_elements(By.CSS_SELECTOR, "#no-results, .no-results"):
            return True
        # Only an explicit empty-result message, not arbitrary text in help panels.
        text = driver.page_source.lower()
        return any(phrase in text for phrase in (
            "no results found", "no matching results", "no papers found"
        ))
    except Exception:
        return False


def _has_results(driver) -> bool:
    from selenium.webdriver.common.by import By
    try:
        return bool(driver.find_elements(By.CSS_SELECTOR, "li.arxiv-result")) or _has_no_results(driver)
    except Exception:
        return False


def _transition_state(driver):
    """A transition must be evidenced by query parameters or actual results DOM."""
    params = parse_qs(urlparse(driver.current_url).query, keep_blank_values=True)
    if "terms-0-term" in params:
        return "url_query"
    if _has_results(driver):
        return "results_dom"
    return False


def _failure_diagnostics(driver) -> dict:
    """Collect bounded, field-specific diagnostics, not a full personal DOM dump."""
    from selenium.webdriver.common.by import By
    out = {}
    try:
        out["url"] = driver.current_url
        out["title"] = str(driver.title)[:180]
    except Exception:
        pass
    try:
        out["headings"] = [x.text[:120] for x in driver.find_elements(By.CSS_SELECTOR, "h1, h2")[:6]]
    except Exception:
        pass
    try:
        out["result_count"] = len(driver.find_elements(By.CSS_SELECTOR, "li.arxiv-result"))
    except Exception:
        pass
    try:
        keyword_nodes = driver.find_elements(By.CSS_SELECTOR, '#terms-0-term, input[name="terms-0-term"]')
        if keyword_nodes:
            keyword = keyword_nodes[0]
            out["keyword_input_value"] = str(keyword.get_property("value") or "")[:150]
            form = keyword.find_element(By.XPATH, "./ancestor::form[1]")
            out["form_action"] = str(form.get_attribute("action") or "")[:230]
            out["form_buttons"] = [_control_description(e) for e in form.find_elements(
                By.CSS_SELECTOR, 'button, input[type="submit"]')[:8]]
    except Exception:
        pass
    return out


def _safe_screenshot(driver, path: Path) -> None:
    try:
        driver.save_screenshot(str(path))
    except Exception:
        pass


def execute_test(contract: dict, locators: dict, base_url: str, output: Path,
                 *, headless: bool = True, demo_delay: float = 0.0) -> dict:
    """One low-frequency public search task, without login, downloads or crawling."""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import Select, WebDriverWait

    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    evidence = {
        "execution_status": "RUNNING", "target": contract["target"],
        "search": {}, "results": {}, "detail": {}, "steps": [],
        "screenshots": [], "error": None, "error_stage": None,
        "collection_log": [], "evidence_plan_version": 1,
    }
    driver = None
    phase = "start"
    demo_delay = min(3.0, max(0.0, float(demo_delay)))
    plan = contract.get("evidence_plan", [])
    shot_prefix = "attempt2_" if output.stem.endswith("attempt2") else ""

    def demo_pause(element=None):
        if demo_delay <= 0 or headless or driver is None:
            return
        if element is not None:
            try:
                driver.execute_script("arguments[0].style.outline='3px solid #ea8b4b'", element)
            except Exception:
                pass
        time.sleep(demo_delay)

    def record(stage: str, **values) -> None:
        evidence["steps"].append({"stage": stage, **values})

    def capture(name: str) -> None:
        if driver is None:
            return
        path = output.parent / (shot_prefix + name + ".png")
        _safe_screenshot(driver, path)
        if path.exists():
            evidence["screenshots"].append(path.name)

    try:
        phase = "browser_launch"
        driver = make_driver(headless=headless)
        phase = "navigate"
        driver.get(base_url)
        record("navigate", url=driver.current_url)
        demo_pause()
        phase = "locate_keyword"
        kw_node = _first_displayed(driver, locators["keyword"])
        phase = "fill_keyword"
        kw_node.clear()
        kw_node.send_keys(contract["query"]["keyword"])
        entered_keyword = kw_node.get_property("value")
        record("fill", keyword=entered_keyword)
        demo_pause(kw_node)
        if contract["query"]["search_field"] != "all":
            phase = "set_search_field"
            field = _first_displayed(driver, locators["field"], timeout=4)
            Select(field).select_by_value(contract["query"]["search_field"])
        if contract["query"].get("computer_science"):
            phase = "set_cs_filter"
            cb = _first_displayed(driver, locators["computer_science"], timeout=4)
            if not cb.is_selected():
                cb.click()
        evidence["search"]["entered_keyword"] = entered_keyword
        evidence["current_url"] = driver.current_url
        collect_checkpoint(evidence, plan, "form_filled", capture=capture,
                           screenshot_name="01_search_form")
        phase = "locate_search_submit"
        button = _form_submit(driver, kw_node, locators.get("submit", []), timeout=12)
        button_info = _control_description(button)
        record("submit_target", button=button_info)
        demo_pause(button)
        phase = "click_search_submit"
        button.click()
        record("submit_clicked", url=driver.current_url)
        phase = "wait_search_transition"
        transition = WebDriverWait(driver, 22).until(_transition_state)
        record("search_transition", signal=transition, url=driver.current_url)
        params = parse_qs(urlparse(driver.current_url).query, keep_blank_values=True)
        # Unknown is distinct from an observed negative: it must never satisfy an oracle.
        applied = params.get("terms-0-term", [None])[0]
        applied_field = params.get("terms-0-field", [None])[0]
        applied_cs = (
            params.get("classification-computer_science", [""])[0].lower() in {"y", "on", "true", "1"}
            if "classification-computer_science" in params else None
        )
        evidence["search"] = {
            "entered_keyword": entered_keyword, "applied_keyword": applied,
            "applied_field": applied_field, "applied_computer_science": applied_cs,
            "results_url": driver.current_url, "submission_signal": transition,
        }
        record("search_submitted", url=driver.current_url, applied_keyword=applied)
        evidence["current_url"] = driver.current_url
        collect_checkpoint(evidence, plan, "search_submitted")
        phase = "wait_results_loaded"
        WebDriverWait(driver, 22).until(_has_results)
        demo_pause()
        items = driver.find_elements(By.CSS_SELECTOR, "li.arxiv-result")
        evidence["results"]["count"] = len(items)
        if not contract["query"].get("open_first_result", True):
            evidence["current_url"] = driver.current_url
            collect_checkpoint(evidence, plan, "results_loaded", capture=capture,
                               screenshot_name="02_search_results")
            record("results_loaded", count=len(items), mode="search_only")
            evidence["execution_status"] = "COMPLETE"
            return evidence
        if not items:
            evidence["current_url"] = driver.current_url
            collect_checkpoint(evidence, plan, "results_loaded", capture=capture,
                               screenshot_name="02_search_results")
            evidence["execution_status"] = "INCONCLUSIVE"
            record("results_loaded", note="no result; cannot verify paper details")
            return evidence
        phase = "read_first_result"
        first = items[0]
        try:
            list_title = first.find_element(By.CSS_SELECTOR, "p.title").text
        except Exception:
            list_title = ""
        links = first.find_elements(By.CSS_SELECTOR, 'a[href*="/abs/"]')
        if not links:
            raise RuntimeError("result item lacks an abstract link")
        link = links[0]
        paper_url = link.get_attribute("href") or ""
        evidence["results"].update({
            "title": list_title, "arxiv_id": normalize_id(paper_url), "paper_url": paper_url
        })
        record("results_loaded", **evidence["results"])
        evidence["current_url"] = driver.current_url
        collect_checkpoint(evidence, plan, "results_loaded", capture=capture,
                           screenshot_name="02_search_results")
        phase = "click_paper_link"
        demo_pause(link)
        link.click()
        phase = "wait_paper_detail"
        WebDriverWait(driver, 18).until(lambda d: "/abs/" in urlparse(d.current_url).path)
        phase = "read_paper_detail"
        detail_title = _first_displayed(driver, ["h1.title", "h1#title", "h1"], timeout=12).text
        detail_title = re.sub(r"^\s*Title\s*:\s*", "", detail_title, flags=re.I)
        evidence["detail"] = {
            "title": detail_title, "arxiv_id": normalize_id(driver.current_url),
            "detail_url": driver.current_url,
        }
        evidence["current_url"] = driver.current_url
        collect_checkpoint(evidence, plan, "detail_loaded", capture=capture,
                           screenshot_name="03_paper_detail")
        demo_pause()
        record("detail_loaded", **evidence["detail"])
        evidence["execution_status"] = "COMPLETE"
        return evidence
    except Exception as exc:
        evidence["execution_status"] = "ERROR"
        evidence["error_stage"] = phase
        error_name = type(exc).__name__
        labels = {
            "locate_search_submit": "未在关键词输入框所属表单内找到可点击的 Search 提交按钮",
            "wait_search_transition": "点击 Search 后，未观察到有效搜索 URL 或结果 DOM",
            "wait_results_loaded": "已提交搜索，但未出现可识别的结果列表或空结果提示",
            "wait_paper_detail": "点击论文后未进入摘要详情页面",
        }
        detail = labels.get(phase, "执行当前浏览器步骤失败")
        evidence["error"] = f"{error_name} [{phase}]: {detail}; {str(exc)[:230]}"
        evidence["traceback_tail"] = traceback.format_exc()[-4500:]
        record("exception", phase=phase, error=evidence["error"])
        if driver is not None:
            evidence["diagnostics"] = _failure_diagnostics(driver)
            (output.parent / "failure_diagnostics.json").write_text(
                json.dumps(evidence["diagnostics"], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            capture("99_execution_error")
        return evidence
    finally:
        output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        if driver is not None:
            driver.quit()
