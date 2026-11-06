"""Bounded JSON transport to the persistent Agent4 browser subprocess.

The process is an isolation/lifecycle boundary, not an OS Python sandbox.
The closed renderer is the code security boundary. No WebDriver is serialized.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from queue import Empty, Queue
import signal
import subprocess
import sys
from threading import Thread
import time

from .dynamic_execution import ExecutionPolicy, ExecutionRejected


class BrowserSession:
    def __init__(self, contract, run_dir: Path, *, policy: ExecutionPolicy, headless=True, root=None):
        contract.assert_integrity()
        policy.authorize(contract.design.target_url, contract.design.target_url)
        self.policy, self.run_dir = policy, Path(run_dir)
        self.deadline = time.monotonic() + policy.total_timeout
        self.closed = False
        self.process = None
        self.messages = Queue()
        root = Path(root) if root else Path(__file__).resolve().parent.parent
        # Do not pass API keys/secrets to a browser execution worker.
        allowed = {'PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'USERPROFILE', 'LOCALAPPDATA',
                   'APPDATA', 'PROGRAMFILES', 'PROGRAMFILES(X86)', 'COMSPEC', 'CHROME_BINARY', 'CHROMEDRIVER_PATH'}
        env = {k: v for k, v in os.environ.items() if k.upper() in allowed}
        env.update(PYTHONPATH=str(root), PYTHONIOENCODING='utf-8', PYTHONDONTWRITEBYTECODE='1')
        self.log = (self.run_dir / 'browser_worker.log').open('w', encoding='utf-8')
        try:
            self.process = subprocess.Popen([sys.executable, '-B', '-m', 'web_agent.browser_worker'], cwd=str(root),
                env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log,
                text=True, encoding='utf-8', bufsize=1,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
                start_new_session=os.name != 'nt')

            def read_messages():
                for line in self.process.stdout:
                    self.messages.put(line)
                self.messages.put(None)

            self.reader = Thread(target=read_messages, daemon=True)
            self.reader.start()
            self.info = self._rpc({'command': 'start', 'contract': contract.to_dict(),
                                  'policy': policy.to_dict(), 'headless': headless,
                                  'run_dir': str(self.run_dir.resolve())}, policy.startup_timeout)
        except Exception:
            self.close(force=True)
            raise

    def remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Dynamic execution total budget exceeded')
        return remaining

    def _rpc(self, request, timeout):
        if self.closed:
            raise ExecutionRejected('Browser session is closed')
        try:
            timeout = min(timeout, self.remaining())
            self.process.stdin.write(json.dumps(request, ensure_ascii=False, allow_nan=False) + '\n')
            self.process.stdin.flush()
            line = self.messages.get(timeout=timeout)
            if line is None:
                raise ExecutionRejected('Browser worker exited unexpectedly')
            response = json.loads(line)
            if not response['ok']:
                raise ExecutionRejected(f"{response['error_type']}: {response['error']}",
                    category=response.get('category', 'environment'))
            return response['result']
        except Empty as exc:
            self.close(force=True)
            raise TimeoutError('Browser worker command timed out; process tree terminated') from exc
        except Exception:
            self.close(force=True)
            raise

    def observe(self, *, timeout=None):
        return self._rpc({'command': 'observe'}, timeout or self.policy.step_timeout)

    def execute(self, artifact, *, timeout=None):
        budget = min(timeout or self.policy.step_timeout, self.remaining())
        return self._rpc({'command': 'execute', 'artifact': {
            k: artifact[k] for k in ('step_id', 'operation', 'source')}, 'timeout': budget}, budget)

    def probe(self, request, artifact, *, timeout=None):
        budget = min(timeout or self.policy.step_timeout, self.remaining())
        return self._rpc({'command': 'probe', 'request': request, 'artifact': {
            k: artifact[k] for k in ('step_id', 'operation', 'source')}, 'timeout': budget}, budget)

    def close(self, force=False):
        if self.closed:
            return
        try:
            if self.process is not None:
                if not force and self.process.poll() is None:
                    try:
                        self.process.stdin.write('{"command":"close"}\n')
                        self.process.stdin.flush()
                        self.process.wait(timeout=3)
                    except (OSError, subprocess.TimeoutExpired):
                        force = True
                if self.process.poll() is None:
                    if os.name == 'nt':
                        subprocess.run(['taskkill', '/PID', str(self.process.pid), '/T', '/F'],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
                            creationflags=subprocess.CREATE_NO_WINDOW)
                    else:
                        os.killpg(self.process.pid, signal.SIGKILL)
                    self.process.wait(timeout=5)
                reader = getattr(self, 'reader', None)
                if reader:
                    reader.join(timeout=1)
                for pipe in (self.process.stdin, self.process.stdout):
                    if pipe:
                        pipe.close()
        finally:
            self.closed = True
            self.log.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
