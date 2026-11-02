"""test_clipboard.py - clipboard.py picks its backend by platform (roadmap P4-03).

clipboard.py is the one clipboard entry point: on native Windows it hands every call
to clipboard_win.py, on macOS it uses its own osascript / NSPasteboard code, and
elsewhere it stops with an error. The platform is faked, so the selection tests run
the same on every OS. The CLI round trip at the end uses the real Windows clipboard
(Windows only; it replaces whatever is on the clipboard).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import clipboard
import clipboard_win

SCRIPT = Path(clipboard.__file__)
STEPS = ('<fmxmlsnippet type="FMObjectList"><Step enable="True" id="89" name="# (comment)">'
         '<Text>a ≠ b ¶</Text></Step></fmxmlsnippet>')


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(sys, 'platform', 'win32')


@pytest.fixture
def macos(monkeypatch):
    monkeypatch.setattr(sys, 'platform', 'darwin')
    # Any call that reaches the Windows backend is a selection bug
    for name in ('detect_clipboard_class', 'read_from_clipboard', 'write_to_clipboard'):
        monkeypatch.setattr(clipboard_win, name, lambda *a: pytest.fail('Windows backend called on macOS'))


@pytest.fixture
def linux(monkeypatch):
    monkeypatch.setattr(sys, 'platform', 'linux')


@pytest.fixture
def snippet(tmp_path):
    path = tmp_path / 'in.xml'
    path.write_text(STEPS, encoding='utf-8')
    return path


def test_backend_names(monkeypatch):
    for platform, expected in (('win32', 'windows'), ('darwin', 'macos'), ('linux', None)):
        monkeypatch.setattr(sys, 'platform', platform)
        assert clipboard.backend() == expected


# ---------------------------------------------------------------------------
# Windows: every call goes to clipboard_win
# ---------------------------------------------------------------------------

def test_windows_write_goes_to_clipboard_win(windows, monkeypatch, snippet):
    calls = []
    monkeypatch.setattr(clipboard_win, 'write_to_clipboard', lambda *a: calls.append(a))
    clipboard.write_to_clipboard(str(snippet), 'XMSC')
    clipboard.write_to_clipboard(str(snippet))
    assert calls == [(str(snippet), 'XMSC'), (str(snippet), None)]


def test_windows_read_goes_to_clipboard_win(windows, monkeypatch):
    calls = []
    monkeypatch.setattr(clipboard_win, 'read_from_clipboard', lambda *a: calls.append(a) or STEPS)
    assert clipboard.read_from_clipboard('out.xml') == STEPS
    assert calls == [('out.xml',)]


def test_windows_detect_goes_to_clipboard_win(windows, monkeypatch):
    monkeypatch.setattr(clipboard_win, 'detect_clipboard_class', lambda: 'XML2')
    assert clipboard.detect_clipboard_class() == 'XML2'


def test_windows_errors_exit_like_the_mac_path(windows, monkeypatch, capsys):
    def fail(*_args):
        raise clipboard_win.ClipboardError('No FileMaker objects found on clipboard.')
    monkeypatch.setattr(clipboard_win, 'read_from_clipboard', fail)
    with pytest.raises(SystemExit) as exc:
        clipboard.read_from_clipboard()
    assert exc.value.code == 1
    assert 'ERROR: No FileMaker objects found on clipboard.' in capsys.readouterr().err


# ---------------------------------------------------------------------------
# macOS: the existing code, never clipboard_win
# ---------------------------------------------------------------------------

def test_macos_detect_uses_nspasteboard(macos, monkeypatch):
    monkeypatch.setattr(clipboard, '_HAS_APPKIT', True)
    monkeypatch.setattr(clipboard, '_nspasteboard_detect', lambda: 'XMSS')
    assert clipboard.detect_clipboard_class() == 'XMSS'


def test_macos_write_uses_nspasteboard(macos, monkeypatch, snippet):
    written = []
    monkeypatch.setattr(clipboard, '_HAS_APPKIT', True)
    monkeypatch.setattr(clipboard, '_nspasteboard_write', lambda cls, data: written.append((cls, data)) or True)
    clipboard.write_to_clipboard(str(snippet))
    # On macOS the file's bytes go on the pasteboard as they are, with no length prefix
    assert written == [('XMSS', snippet.read_bytes())]


def test_macos_detect_falls_back_to_osascript(macos, monkeypatch):
    commands = []

    def fake_run(cmd, **_kwargs):
        commands.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout='«class XMSC»\n', stderr='')

    monkeypatch.setattr(clipboard, '_HAS_APPKIT', False)
    monkeypatch.setattr(clipboard.subprocess, 'run', fake_run)
    assert clipboard.detect_clipboard_class() == 'XMSC'
    assert commands[0][0] == 'osascript'


# ---------------------------------------------------------------------------
# Other platforms: a clear error, no osascript
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('call', ['detect', 'read', 'write'])
def test_linux_has_no_backend(linux, monkeypatch, capsys, snippet, call):
    monkeypatch.setattr(clipboard.subprocess, 'run', lambda *a, **k: pytest.fail('ran a command on Linux'))
    with pytest.raises(SystemExit) as exc:
        if call == 'detect':
            clipboard.detect_clipboard_class()
        elif call == 'read':
            clipboard.read_from_clipboard()
        else:
            clipboard.write_to_clipboard(str(snippet))
    assert exc.value.code == 1
    assert 'No clipboard backend for linux' in capsys.readouterr().err


# ---------------------------------------------------------------------------
# The real CLI on Windows
# ---------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform != 'win32', reason='Win32 clipboard')
def test_cli_round_trip_on_windows(tmp_path, snippet):
    out = tmp_path / 'out.xml'
    write = subprocess.run([sys.executable, str(SCRIPT), 'write', str(snippet)],
                           capture_output=True, text=True, encoding='utf-8')
    assert write.returncode == 0, write.stderr
    assert 'Mac-XMSS' in write.stderr

    read = subprocess.run([sys.executable, str(SCRIPT), 'read', str(out)],
                          capture_output=True, text=True, encoding='utf-8')
    assert read.returncode == 0, read.stderr
    assert 'a ≠ b ¶' in out.read_text(encoding='utf-8')
