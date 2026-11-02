"""test_companion_trigger.py - the companion's /trigger runs a script through an fmp:// URL or AppleScript (roadmap P4-05).

The handler is built without a socket, and opening the URL or running osascript is
faked, so these tests run the same on every OS.
"""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.parse

import pytest

import companion_server as cs


def _handler(body: dict):
    handler = cs.CompanionHandler.__new__(cs.CompanionHandler)
    handler.sent = []
    handler._send_json = lambda data, status=200: handler.sent.append((status, data))
    handler._read_body = lambda: json.dumps(body).encode('utf-8')
    return handler


@pytest.fixture(autouse=True)
def no_pending_job(monkeypatch):
    monkeypatch.setattr(cs, '_pending_job', None)


@pytest.fixture
def opened(monkeypatch):
    """Records each URL handed to the OS instead of opening it."""
    urls = []
    monkeypatch.setattr(cs, '_open_fmp_url', urls.append)
    return urls


@pytest.fixture
def osascript(monkeypatch):
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, 'ok\n', '')
    monkeypatch.setattr(cs.subprocess, 'run', run)
    return calls


# --- the URL ---

def test_url_for_a_plain_script():
    assert cs.fmp_script_url('Clients', 'ListClients') == 'fmp://$/Clients?script=ListClients'


def test_url_encodes_file_script_and_parameter():
    url = cs.fmp_script_url('My Addresses.fmp12', 'Test & Run = 1', 'a/b?c#d&e=f g+h%i')
    assert url == ('fmp://$/My%20Addresses.fmp12?script=Test%20%26%20Run%20%3D%201'
                   '&param=a%2Fb%3Fc%23d%26e%3Df%20g%2Bh%25i')


def test_url_encodes_non_ascii_as_utf8():
    url = cs.fmp_script_url('Café', 'Größe ≠', 'a ≠ b ¶ café')
    assert url == 'fmp://$/Caf%C3%A9?script=Gr%C3%B6%C3%9Fe%20%E2%89%A0&param=a%20%E2%89%A0%20b%20%C2%B6%20caf%C3%A9'


def test_url_leaves_out_an_empty_parameter():
    assert '&param' not in cs.fmp_script_url('Clients', 'ListClients', '')


@pytest.mark.parametrize('text', ['a & b = c', 'x?y#z/w', '100% ≠ ¶ é', ' leading and trailing ', 'line\nbreak'])
def test_every_part_decodes_back_to_the_original(text):
    url = cs.fmp_script_url(text, text, text)
    path, _, query = url[len('fmp://$/'):].partition('?')
    assert urllib.parse.unquote(path) == text
    pairs = [p.split('=', 1) for p in query.split('&')]
    assert [k for k, _ in pairs] == ['script', 'param']
    assert [urllib.parse.unquote(v) for _, v in pairs] == [text, text]


# --- choosing the backend ---

@pytest.mark.parametrize('platform', ['win32', 'linux'])
def test_url_is_the_default_off_macos(platform, monkeypatch, opened):
    monkeypatch.setattr(sys, 'platform', platform)
    handler = _handler({'script': 'Run Tests', 'target_file': 'Solution', 'parameter': 'p'})
    handler._handle_trigger()

    assert opened == ['fmp://$/Solution?script=Run%20Tests&param=p']
    assert handler.sent == [(200, {'success': True, 'backend': 'url', 'url': opened[0]})]


def test_applescript_stays_the_default_on_macos(monkeypatch, opened, osascript):
    monkeypatch.setattr(sys, 'platform', 'darwin')
    handler = _handler({'script': 'Run Tests', 'target_file': 'Solution'})
    handler._handle_trigger()

    assert opened == []
    (cmd,) = osascript
    assert cmd[:2] == ['osascript', '-e']
    assert 'do script "Run Tests"' in cmd[2]
    assert 'first document whose name contains "Solution"' in cmd[2]
    assert handler.sent == [(200, {'success': True, 'backend': 'applescript', 'stdout': 'ok', 'stderr': ''})]


def test_url_backend_can_be_chosen_on_macos(monkeypatch, opened, osascript):
    monkeypatch.setattr(sys, 'platform', 'darwin')
    handler = _handler({'script': 'Run Tests', 'target_file': 'Solution', 'backend': 'url'})
    handler._handle_trigger()

    assert opened == ['fmp://$/Solution?script=Run%20Tests']
    assert osascript == []


def test_raw_applescript_uses_applescript_on_any_platform(monkeypatch, opened, osascript):
    monkeypatch.setattr(sys, 'platform', 'win32')
    handler = _handler({'raw_applescript': 'beep'})
    handler._handle_trigger()

    assert osascript == [['osascript', '-e', 'beep']]
    assert opened == []


# --- refusals ---

@pytest.mark.parametrize('body, error', [
    ({'script': 'S', 'target_file': 'F', 'backend': 'shell'}, 'Unknown backend'),
    ({'raw_applescript': 'beep', 'backend': 'url'}, 'raw_applescript needs the applescript backend'),
    ({'target_file': 'F', 'backend': 'url'}, 'Missing required field: script'),
    ({'script': 'S', 'backend': 'url'}, 'needs target_file'),
])
def test_bad_requests_are_refused(body, error, opened, osascript):
    handler = _handler(body)
    handler._handle_trigger()

    ((status, data),) = handler.sent
    assert status == 400
    assert error in data['error']
    assert opened == [] and osascript == []


def test_a_url_the_os_cannot_open_is_reported(monkeypatch):
    def fail(url):
        raise OSError('no application is associated with fmp')
    monkeypatch.setattr(cs, '_open_fmp_url', fail)
    handler = _handler({'script': 'S', 'target_file': 'F', 'backend': 'url'})
    handler._handle_trigger()

    ((status, data),) = handler.sent
    assert status == 500
    assert data['success'] is False and data['backend'] == 'url'
    assert 'no application is associated with fmp' in data['error']


# --- the pending job ---

def test_url_backend_also_sets_the_pending_job(opened):
    handler = _handler({'script': 'Agentic-fm Paste', 'target_file': 'F', 'backend': 'url',
                        'parameter': 'Target Script', 'auto_save': True, 'select_all': False})
    handler._handle_trigger()

    assert cs._pending_job == {'target': 'Target Script', 'auto_save': True, 'select_all': False}
    assert opened == ['fmp://$/F?script=Agentic-fm%20Paste&param=Target%20Script']


def test_no_parameter_leaves_the_pending_job_alone(opened):
    _handler({'script': 'S', 'target_file': 'F', 'backend': 'url'})._handle_trigger()
    assert cs._pending_job is None


# --- opening the URL ---

def test_open_on_windows_uses_startfile(monkeypatch):
    opened = []
    monkeypatch.setattr(sys, 'platform', 'win32')
    monkeypatch.setattr(cs.os, 'startfile', opened.append, raising=False)
    cs._open_fmp_url('fmp://$/F?script=S')
    assert opened == ['fmp://$/F?script=S']


def test_open_on_macos_uses_open(monkeypatch):
    calls = []
    monkeypatch.setattr(sys, 'platform', 'darwin')
    monkeypatch.setattr(cs.subprocess, 'run',
                        lambda cmd, **kw: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, '', ''))
    cs._open_fmp_url('fmp://$/F?script=S')
    assert calls == [['open', 'fmp://$/F?script=S']]


def test_open_on_macos_reports_a_failure(monkeypatch):
    monkeypatch.setattr(sys, 'platform', 'darwin')
    monkeypatch.setattr(cs.subprocess, 'run',
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, '', 'No application knows how to open URL'))
    with pytest.raises(OSError, match='No application knows'):
        cs._open_fmp_url('fmp://$/F?script=S')


def test_open_on_linux_is_refused(monkeypatch):
    monkeypatch.setattr(sys, 'platform', 'linux')
    with pytest.raises(OSError, match='Windows or macOS'):
        cs._open_fmp_url('fmp://$/F?script=S')
