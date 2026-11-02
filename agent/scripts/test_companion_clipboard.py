"""test_companion_clipboard.py - the companion's /clipboard goes through clipboard.py (roadmap P4-03).

GET and POST /clipboard run clipboard.py on every platform; clipboard.py picks the
macOS or Windows backend. The handler is built without a socket and subprocess.run
is faked, so these tests run the same on every OS. The last test runs the real
clipboard.py on Windows (it replaces whatever is on the clipboard).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

import companion_server as cs

STEPS = ('<fmxmlsnippet type="FMObjectList"><Step enable="True" id="89" name="# (comment)">'
         '<Text>a ≠ b ¶</Text></Step></fmxmlsnippet>')
CLIPBOARD_PY = os.path.join(os.path.dirname(os.path.abspath(cs.__file__)), 'clipboard.py')


def _handler(body: dict | None = None):
    handler = cs.CompanionHandler.__new__(cs.CompanionHandler)
    handler.sent = []
    handler._send_json = lambda data, status=200: handler.sent.append((status, data))
    handler._read_body = lambda: json.dumps(body or {}).encode('utf-8')
    return handler


class FakeRun:
    """Records each subprocess.run call and answers with a fixed result."""

    def __init__(self, returncode=0, stdout='', stderr=''):
        self.calls = []
        self.result = (returncode, stdout, stderr)
        self.files = []

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        if cmd[2] == 'write':
            with open(cmd[3], encoding='utf-8') as f:
                self.files.append(f.read())
        return subprocess.CompletedProcess(cmd, *self.result)


@pytest.fixture(params=['win32', 'darwin', 'linux'])
def any_platform(request, monkeypatch):
    monkeypatch.setattr(sys, 'platform', request.param)
    monkeypatch.setattr(cs.platform, 'system', lambda: {'win32': 'Windows', 'darwin': 'Darwin'}.get(
        request.param, 'Linux'))
    return request.param


def test_write_runs_clipboard_py_on_every_platform(any_platform, monkeypatch):
    run = FakeRun()
    monkeypatch.setattr(cs.subprocess, 'run', run)
    handler = _handler({'xml': STEPS})
    handler._handle_clipboard()

    (cmd, kwargs), = run.calls
    assert cmd == [sys.executable, CLIPBOARD_PY, 'write', cmd[3]]
    assert kwargs['encoding'] == 'utf-8'
    assert run.files == [STEPS]
    assert not os.path.exists(cmd[3])
    assert handler.sent == [(200, {'success': True})]


def test_read_runs_clipboard_py_on_every_platform(any_platform, monkeypatch):
    run = FakeRun(stdout=STEPS + '\n')
    monkeypatch.setattr(cs.subprocess, 'run', run)
    handler = _handler()
    handler._handle_clipboard_read()

    (cmd, kwargs), = run.calls
    assert cmd == [sys.executable, CLIPBOARD_PY, 'read']
    assert kwargs['encoding'] == 'utf-8'
    assert handler.sent == [(200, {'success': True, 'xml': STEPS + '\n'})]


def test_read_with_nothing_on_the_clipboard_is_404(monkeypatch):
    monkeypatch.setattr(cs.subprocess, 'run',
                        FakeRun(1, stderr='ERROR: No FileMaker objects found on clipboard.\n'))
    handler = _handler()
    handler._handle_clipboard_read()
    assert handler.sent == [(404, {'success': False,
                                   'error': 'ERROR: No FileMaker objects found on clipboard.'})]


def test_unsupported_platform_error_is_passed_on(monkeypatch):
    error = 'ERROR: No clipboard backend for linux: clipboard.py works on macOS and native Windows.\n'
    monkeypatch.setattr(cs.subprocess, 'run', FakeRun(1, stderr=error))
    handler = _handler({'xml': STEPS})
    handler._handle_clipboard()
    assert handler.sent == [(500, {'success': False, 'error': error})]


def test_write_without_xml_is_400(monkeypatch):
    monkeypatch.setattr(cs.subprocess, 'run', lambda *a, **k: pytest.fail('ran clipboard.py'))
    handler = _handler({})
    handler._handle_clipboard()
    assert handler.sent[0][0] == 400


@pytest.mark.skipif(sys.platform != 'win32', reason='Win32 clipboard')
def test_write_then_read_on_windows():
    writer = _handler({'xml': STEPS})
    writer._handle_clipboard()
    assert writer.sent == [(200, {'success': True})]

    reader = _handler()
    reader._handle_clipboard_read()
    (status, data), = reader.sent
    assert status == 200, data
    # Decoded as UTF-8, not the ANSI code page
    assert 'a ≠ b ¶' in data['xml']
