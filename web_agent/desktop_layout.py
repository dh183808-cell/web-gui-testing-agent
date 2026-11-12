"""Optional Windows presentation layout. Never part of generated Selenium code."""
from contextlib import contextmanager
from copy import deepcopy
import ctypes
from ctypes import wintypes as W
import json
import os
from pathlib import Path
import sys
import time


def split_work_area(work):
    """All coordinates are physical pixels, including negative monitor origins."""
    x, y, width, height = (work[k] for k in ('x', 'y', 'width', 'height'))
    if any(type(v) is not int for v in (x, y, width, height)) or width < 2 or height < 1:
        raise ValueError('Invalid monitor work area')
    half = width // 2
    return dict(left=dict(x=x, y=y, width=half, height=height),
                right=dict(x=x+half, y=y, width=width-half, height=height))


def _user32():
    if os.name != 'nt':
        raise OSError('Windows interactive desktop required')
    api = ctypes.WinDLL('user32', use_last_error=True)
    api.IsWindow.argtypes = [W.HWND]
    api.IsWindowVisible.argtypes = [W.HWND]
    api.GetWindowRect.argtypes = [W.HWND, ctypes.POINTER(W.RECT)]
    api.GetWindowThreadProcessId.argtypes = [W.HWND, ctypes.POINTER(W.DWORD)]
    api.ShowWindow.argtypes = [W.HWND, ctypes.c_int]
    api.SetWindowPos.argtypes = [W.HWND, W.HWND, ctypes.c_int, ctypes.c_int,
                                 ctypes.c_int, ctypes.c_int, W.UINT]
    api.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    api.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
    return api


@contextmanager
def physical_pixels():
    api = _user32()
    previous = api.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))  # per-monitor v2
    if not previous:
        raise OSError('Per-monitor DPI context unavailable')
    try:
        yield api
    finally:
        api.SetThreadDpiAwarenessContext(previous)


def monitors():
    class Info(ctypes.Structure):
        _fields_ = [('size', W.DWORD), ('monitor', W.RECT), ('work', W.RECT),
                    ('flags', W.DWORD), ('device', W.WCHAR * 32)]
    result = []
    with physical_pixels() as api:
        api.GetMonitorInfoW.argtypes = [W.HANDLE, ctypes.POINTER(Info)]
        callback_type = ctypes.WINFUNCTYPE(W.BOOL, W.HANDLE, W.HDC,
                                          ctypes.POINTER(W.RECT), W.LPARAM)
        def collect(handle, dc, rect, data):
            info = Info(); info.size = ctypes.sizeof(info)
            if api.GetMonitorInfoW(handle, ctypes.byref(info)):
                r = info.work
                result.append(dict(device=info.device, primary=bool(info.flags & 1),
                    work=dict(x=r.left, y=r.top, width=r.right-r.left, height=r.bottom-r.top)))
            return True
        if not api.EnumDisplayMonitors(None, None, callback_type(collect), 0):
            raise OSError('Cannot enumerate displays')
    return sorted(result, key=lambda m: (not m['primary'], m['device']))


def window_info(hwnd):
    with physical_pixels() as api:
        r = W.RECT()
        if not api.IsWindow(hwnd) or not api.GetWindowRect(hwnd, ctypes.byref(r)):
            raise OSError('Window is unavailable')
        dpi = None
        if hasattr(api, 'GetDpiForWindow'):
            api.GetDpiForWindow.argtypes = [W.HWND]
            dpi = api.GetDpiForWindow(hwnd)
        return dict(hwnd=int(hwnd), visible=bool(api.IsWindowVisible(hwnd)), dpi=dpi,
                    rect=dict(x=r.left, y=r.top, width=r.right-r.left, height=r.bottom-r.top))


def place_window(hwnd, rect):
    with physical_pixels() as api:
        api.ShowWindow(hwnd, 9)  # restore maximized windows
        if not api.SetWindowPos(hwnd, None, rect['x'], rect['y'], rect['width'], rect['height'], 0x0040):
            raise OSError('Cannot position window')
    info = window_info(hwnd)
    info['positioned'] = info['visible'] and all(abs(info['rect'][k]-rect[k]) <= 16 for k in rect)
    if not info['positioned']:
        info['warning'] = 'Window minimum size, decoration or DPI prevented the requested rectangle'
    return info


def process_windows(root_pid):
    """Visible Chrome windows belonging to an owned process tree (no title guessing)."""
    class Process(ctypes.Structure):
        _fields_ = [('size',W.DWORD),('usage',W.DWORD),('pid',W.DWORD),('heap',ctypes.c_size_t),
                    ('module',W.DWORD),('threads',W.DWORD),('parent',W.DWORD),
                    ('priority',ctypes.c_long),('flags',W.DWORD),('exe',W.WCHAR*260)]
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes=[W.DWORD,W.DWORD]
    kernel.CreateToolhelp32Snapshot.restype=W.HANDLE
    kernel.Process32FirstW.argtypes=[W.HANDLE,ctypes.POINTER(Process)]
    kernel.Process32NextW.argtypes=[W.HANDLE,ctypes.POINTER(Process)]
    kernel.CloseHandle.argtypes=[W.HANDLE]
    snapshot=kernel.CreateToolhelp32Snapshot(2,0)
    if snapshot==ctypes.c_void_p(-1).value: raise OSError('Cannot inspect owned browser process')
    parents={}
    try:
        row=Process();row.size=ctypes.sizeof(row)
        valid=kernel.Process32FirstW(snapshot,ctypes.byref(row))
        while valid:
            parents[row.pid]=row.parent
            valid=kernel.Process32NextW(snapshot,ctypes.byref(row))
    finally: kernel.CloseHandle(snapshot)
    owned={int(root_pid)}
    while True:
        expanded=owned|{pid for pid,parent in parents.items() if parent in owned}
        if expanded==owned: break
        owned=expanded
    handles = []
    with physical_pixels() as api:
        callback_type = ctypes.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
        def collect(hwnd, data):
            pid = W.DWORD()
            api.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value in owned and api.IsWindowVisible(hwnd):
                name = ctypes.create_unicode_buffer(256)
                api.GetClassNameW.argtypes = [W.HWND, W.LPWSTR, ctypes.c_int]
                api.GetClassNameW(hwnd, name, 256)
                if name.value == 'Chrome_WidgetWin_1': handles.append(int(hwnd))
            return True
        api.EnumWindows(callback_type(collect), 0)
    return handles


def chrome_window(driver):
    """Start from this driver's service PID; never touch an unrelated Chrome."""
    handles=process_windows(driver.service.process.pid)
    if len(handles) != 1:
        raise OSError('No unique visible window for this Chrome process')
    return handles[0]


def place_chrome(driver, rect):
    """Best effort only; placement errors cannot become business verdicts."""
    result = dict(requested=deepcopy(rect), positioned=False)
    try:
        # Native positioning avoids WebDriver's logical-coordinate differences at 125/150% DPI.
        result.update(place_window(chrome_window(driver), rect))
    except Exception as exc:
        result['warning'] = 'Native layout unavailable: ' + type(exc).__name__
        try:
            driver.set_window_rect(**rect)
            result['webdriver_rect'] = driver.get_window_rect()
        except Exception as fallback:
            result['fallback_error_type'] = type(fallback).__name__
    return result


def freeze_presentation(mode='normal', layout=None):
    if mode not in {'normal', 'demo'}:
        raise ValueError('运行模式必须是 normal 或 demo')
    if mode == 'normal': return dict(mode=mode)
    if layout is None:
        try:
            layout = json.loads(os.environ.get('WEBAGENT_DEMO_LAYOUT', '{}'))
            if not layout:
                selected = monitors()[0]
                layout = dict(monitor=selected['device'], **split_work_area(selected['work']))
        except (OSError, ValueError, IndexError):
            layout = {}
    clean = dict(mode=mode, terminal_output='streamlit_stdout')
    for side in ('left', 'right'):
        if side in layout:
            try:
                rect = {k: layout[side][k] for k in ('x', 'y', 'width', 'height')}
                split_work_area(rect)  # strict types and positive size
                clean[side] = rect
            except (KeyError, TypeError, ValueError):
                pass  # An invalid display rectangle cannot alter test semantics.
    for key in ('terminal_hwnd', 'frontend_hwnd'):
        if type(layout.get(key)) is int: clean[key] = layout[key]
    clean['monitor'] = str(layout.get('monitor', 'unavailable'))[:80]
    clean['desktop_ready'] = False
    try:
        clean['desktop_ready'] = all(window_info(clean[k])['visible']
                                     for k in ('terminal_hwnd', 'frontend_hwnd'))
    except (KeyError, OSError):
        pass
    if 'right' not in clean:
        clean['warning'] = '无法确定显示器右半屏区域；日志仍输出到启动 Streamlit 的终端，测试 Chrome 使用默认窗口位置。'
    return deepcopy(clean)


def session_display(presentation, purpose, reason, headless=True, formal_headless=None):
    """Legacy CLI headless flags retain their semantics in normal mode."""
    if presentation['mode'] != 'demo':
        return dict(headless=headless if purpose != 'formal' or formal_headless is None else formal_headless)
    cold = reason == 'mandatory_cold_trial'
    return dict(headless=cold, **({'window_rect':deepcopy(presentation['right'])}
                                 if not cold and presentation.get('right') else {}),
                presentation_events=not cold)


def prepare_console(monitor_index=0):
    """Classic conhost has a controllable HWND; pseudoconsole hosts may not."""
    selected = monitors()[monitor_index]
    layout = dict(monitor=selected['device'], **split_work_area(selected['work']))
    kernel = ctypes.WinDLL('kernel32'); kernel.GetConsoleWindow.restype = W.HWND
    terminal = int(kernel.GetConsoleWindow() or 0)
    placed = place_window(terminal, layout['left'])
    if not placed['positioned']:
        raise OSError('Terminal cannot be placed; use the conhost launcher')
    return dict(layout, terminal_hwnd=terminal)


def launch(monitor_index=0, port=8501):
    """Own only the presentation UI browser and server; tests still own their sessions."""
    import subprocess
    import tempfile
    import urllib.request
    from .selenium_runtime import make_driver
    layout = prepare_console(monitor_index)
    root = Path(__file__).resolve().parents[1]
    server = browser = None
    with tempfile.TemporaryDirectory(prefix='webagent-demo-ui-') as profile:
        try:
            browser = make_driver(headless=False, profile_dir=Path(profile), window_rect=layout['right'])
            positioned = place_chrome(browser, layout['right'])
            if not positioned['positioned']: raise OSError(positioned.get('warning', 'Frontend layout failed'))
            layout.update(frontend_hwnd=positioned['hwnd'])
            env = dict(os.environ, WEBAGENT_DEMO_LAYOUT=json.dumps(layout), PYTHONIOENCODING='utf-8')
            # Refuse an existing service: it would not inherit the prepared desktop context.
            import socket
            with socket.socket() as probe:
                probe.bind(('127.0.0.1', port))
            server = subprocess.Popen([sys.executable, '-m', 'streamlit', 'run', str(root/'app_ui.py'),
                '--server.address', '127.0.0.1', '--server.port', str(port),
                '--server.headless', 'true', '--browser.gatherUsageStats', 'false'],
                cwd=root, env=env, creationflags=subprocess.CREATE_NO_WINDOW)
            url = f'http://127.0.0.1:{port}'
            for _ in range(120):
                if server.poll() is not None: raise OSError('Streamlit exited')
                try:
                    with urllib.request.urlopen(url+'/_stcore/health', timeout=.3) as response:
                        if response.status == 200: break
                except OSError: time.sleep(.25)
            else: raise OSError('Streamlit startup timed out')
            browser.get(url)
            print('WebAgent | Desktop ready. Select 演示模式 in the UI. Ctrl+C stops the launcher.', flush=True)
            while server.poll() is None:
                time.sleep(.5)
        except KeyboardInterrupt:
            pass
        finally:
            if browser:
                try: browser.quit()
                except Exception: pass
            if server and server.poll() is None:
                server.terminate()
                try: server.wait(timeout=5)
                except subprocess.TimeoutExpired: server.kill(); server.wait(timeout=5)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Prepare WebAgent presentation desktop; no model calls')
    parser.add_argument('--monitor', type=int, default=0)
    parser.add_argument('--port', type=int, default=8501)
    args = parser.parse_args()
    try:
        launch(args.monitor, args.port)
    except Exception as exc:
        from .demo_console import safe_display
        print('Desktop preparation failed: '+safe_display(str(exc)), flush=True)
        print('No task has been started. Check display index, Chrome/Driver and an unused port.',flush=True)
        if sys.stdin.isatty(): input('Press Enter to close…')
        raise SystemExit(1)
