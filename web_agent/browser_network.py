"""Chrome CDP pre-request origin guard (independent of Selenium BiDi versions).

Attach the browser debugger to every target, pausing new contexts before script
execution. Only the original page is resumed, with Fetch interception enabled.
Other pages/workers/frames are closed, not allowed to send unguarded requests.
The WebSocket exposes no interface to generated code or the model.
"""
from __future__ import annotations

import json
import ipaddress
from queue import Empty, Queue
from threading import Event, Lock, Thread
from urllib.request import urlopen
from urllib.parse import urlsplit, urlunsplit

from .dynamic_execution import ExecutionRejected


class OriginGuard:
    def __init__(self, driver, policy, target_url, *, timeout=5):
        import websocket
        self.policy, self.target_url = policy, target_url
        self.main_target = driver.current_window_handle
        self.blocked, self.errors = [], []
        self.closed = False
        self.lock, self.ready = Lock(), Event()
        self.counter, self.pending, self.resume = 0, {}, {}
        self.socket = None
        address = driver.capabilities['goog:chromeOptions']['debuggerAddress']
        try:
            with urlopen(f'http://{address}/json/version', timeout=timeout) as response:
                endpoint = json.load(response)['webSocketDebuggerUrl']
            # Chrome may advertise localhost, resolved to IPv6 while this driver
            # listens only on IPv4. Bind the returned debugger endpoint to the
            # exact loopback debugger address, never an arbitrary page URL.
            host = urlsplit('http://' + address).hostname
            if not (host == 'localhost' or ipaddress.ip_address(host).is_loopback):
                raise ExecutionRejected('Debugger endpoint must be loopback')
            parts = urlsplit(endpoint)
            endpoint = urlunsplit((parts.scheme, address, parts.path, '', ''))
            self.socket = websocket.create_connection(endpoint, timeout=timeout, suppress_origin=True,
                                                       http_no_proxy=['localhost', '127.0.0.1', '::1'])
            self.socket.settimeout(0.5)
            self.reader = Thread(target=self._read, daemon=True)
            self.reader.start()
            self._command('Target.setAutoAttach', {'autoAttach': True,
                'waitForDebuggerOnStart': True, 'flatten': True}, timeout=timeout)
            if not self.ready.wait(timeout) or self.errors:
                raise ExecutionRejected('Cannot establish pre-request network interception')
        except Exception:
            self.close()
            raise

    def _send(self, method, params, session=None, *, reply=None, resume=None):
        with self.lock:
            self.counter += 1
            message = {'id': self.counter, 'method': method, 'params': params}
            if session:
                message['sessionId'] = session
            if reply is not None:
                self.pending[self.counter] = reply
            if resume is not None:
                self.resume[self.counter] = resume
            self.socket.send(json.dumps(message))
            return self.counter

    def _command(self, method, params, *, timeout):
        queue = Queue()
        self._send(method, params, reply=queue)
        try:
            response = queue.get(timeout=timeout)
        except Empty as exc:
            raise ExecutionRejected('Network guard command timed out') from exc
        if 'error' in response:
            raise ExecutionRejected('Network guard command rejected by Chrome')

    def _read(self):
        import websocket
        while not self.closed:
            try:
                raw = self.socket.recv()
                if not raw:
                    break
                message = json.loads(raw)
                if 'id' in message:
                    key = message['id']
                    if 'error' in message:
                        self.errors.append('Chrome rejected a network policy command')
                    if key in self.pending:
                        self.pending.pop(key).put(message)
                    if key in self.resume:
                        session = self.resume.pop(key)
                        if 'error' not in message:
                            self._send('Runtime.runIfWaitingForDebugger', {}, session)
                            self.ready.set()
                    continue
                self._event(message)
            except websocket.WebSocketTimeoutException:
                continue
            except Exception:
                if not self.closed:
                    self.errors.append('Network guard connection failed')
                break
        if not self.closed:
            self.errors.append('Network guard disconnected')

    def _event(self, message):
        params, method = message.get('params', {}), message.get('method')
        if method == 'Target.attachedToTarget':
            info, session = params['targetInfo'], params['sessionId']
            if info['type'] == 'browser_ui' and info.get('url', '').startswith('chrome://'):
                # Chrome 156 exposes its omnibox UI as debugger targets. These
                # browser-owned controls are not website browsing contexts.
                self._send('Target.detachFromTarget', {'sessionId': session})
                return
            if info['targetId'] != self.main_target:
                self.blocked.append('New browsing/worker context blocked')
                self._send('Target.closeTarget', {'targetId': info['targetId']})
                return
            # Child workers/OOPIFs must also pause before running. setAutoAttach
            # is per-session, not recursively inherited from the browser target.
            self._send('Target.setAutoAttach', {'autoAttach': True,
                'waitForDebuggerOnStart': True, 'flatten': True}, session)
            self._send('Network.enable', {}, session)
            self._send('Network.setBlockedURLs', {'urls': ['ws://*', 'wss://*', 'ftp://*']}, session)
            self._send('Fetch.enable', {'patterns': [{'urlPattern': '*', 'requestStage': 'Request'}]},
                       session, resume=session)
        elif method == 'Fetch.requestPaused':
            url, request_id = params['request']['url'], params['requestId']
            session = message['sessionId']
            try:
                self.policy.authorize(url, self.target_url)
            except Exception:
                self.blocked.append('Request outside reviewed origin/scheme blocked')
                self._send('Fetch.failRequest', {'requestId': request_id, 'errorReason': 'BlockedByClient'}, session)
            else:
                self._send('Fetch.continueRequest', {'requestId': request_id}, session)

    def close(self):
        self.closed = True
        if self.socket is not None:
            self.socket.close()
        reader = getattr(self, 'reader', None)
        if reader:
            reader.join(timeout=1)
