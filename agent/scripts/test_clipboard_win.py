"""test_clipboard_win.py - native Windows clipboard module (roadmap P4-02).

The encoding tests run on every OS: they check the payload rules in
agent/docs/CLIPBOARD_FORMATS.md against payloads shaped like FMCuttingBoard's captures.
The round-trip tests call the Win32 clipboard and run on Windows only; they replace
whatever is on the clipboard.
"""

from __future__ import annotations

import struct
import subprocess
import sys
from pathlib import Path

import pytest

import clipboard_win as cw

SCRIPT = Path(cw.__file__)

STEPS = ('<fmxmlsnippet type="FMObjectList"><Step enable="True" id="89" name="# (comment)">'
         '<Text>a ≠ b ¶ café</Text></Step></fmxmlsnippet>')
LAYOUT = ('<?xml version="1.0" encoding="UTF-8"?>\n<fmxmlsnippet type="LayoutObjectList">\n'
          '<Layout enclosingRectTop ="141.0000000"></Layout>\n</fmxmlsnippet>')
MENU = ('<FMObjectTransfer><CustomMenu id="1" name="M"><MenuItemList><MenuItem>'
        '<Step id="89" name="# (comment)"></Step></MenuItem></MenuItemList></CustomMenu></FMObjectTransfer>')

SNIPPETS = {
    'XMSS': STEPS,
    'XMSC': '<fmxmlsnippet type="FMObjectList"><Script id="1" name="S"><Step id="89"/></Script></fmxmlsnippet>',
    'XMFD': '<fmxmlsnippet type="FMObjectList"><Field id="2" dataType="Text" name="F"></Field></fmxmlsnippet>',
    'XMTB': '<fmxmlsnippet type="FMObjectList"><BaseTable name="T"><Field id="1" name="A"/></BaseTable></fmxmlsnippet>',
    'XMFN': '<fmxmlsnippet type="FMObjectList"><CustomFunction id="1" name="CF"></CustomFunction></fmxmlsnippet>',
    'XMVL': '<fmxmlsnippet type="FMObjectList"><ValueList id="1" name="VL"></ValueList></fmxmlsnippet>',
    'XML2': LAYOUT,
}


def _prefixed(text: str) -> bytes:
    body = text.encode('utf-8')
    return struct.pack('<I', len(body)) + body


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------

def test_payload_is_little_endian_byte_length_then_utf8():
    data = cw.encode_payload('XMSS', STEPS)
    body = STEPS.encode('utf-8')
    assert data[:4] == struct.pack('<I', len(body))
    assert data[4:] == body
    # The prefix counts bytes, not characters: the non-ASCII characters take 2 or 3 bytes
    assert struct.unpack('<I', data[:4])[0] > len(STEPS)


def test_payload_has_no_bom_and_no_trailing_nul():
    data = cw.encode_payload('XMSS', '﻿' + STEPS)
    assert data[4:7] != b'\xef\xbb\xbf'
    assert not data.endswith(b'\x00')


def test_declaration_dropped_except_for_layout_objects():
    with_decl = '<?xml version="1.0" encoding="UTF-8"?>\n' + STEPS
    assert cw.encode_payload('XMSS', with_decl)[4:].startswith(b'<fmxmlsnippet')

    no_decl = LAYOUT.split('\n', 1)[1]
    for text in (LAYOUT, no_decl):
        body = cw.encode_payload('XML2', text)[4:]
        assert body.startswith(b'<?xml version="1.0" encoding="UTF-8"?>\n<fmxmlsnippet')
        assert body.count(b'<?xml') == 1


def test_crlf_and_cr_become_lf():
    body = cw.encode_payload('XML2', LAYOUT.replace('\n', '\r\n') + '\r')[4:]
    assert b'\r' not in body
    assert body.count(b'\n') == LAYOUT.count('\n') + 1


def test_menu_payload_is_utf16_with_bom_and_no_prefix():
    data = cw.encode_payload('ut16', '<?xml version="1.0"?>\n' + MENU)
    assert data[:2] == b'\xff\xfe'
    assert data.decode('utf-16') == MENU


@pytest.mark.parametrize('cls', sorted(SNIPPETS))
def test_encode_then_decode_returns_the_xml(cls):
    text = SNIPPETS[cls]
    assert cw.decode_payload(cls, cw.encode_payload(cls, text)) == text


# ---------------------------------------------------------------------------
# Decoding
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('cls', sorted(SNIPPETS))
def test_decodes_filemaker_shaped_payloads(cls):
    # As captured: prefix == size - 4, declaration only in XML2
    assert cw.decode_payload(cls, _prefixed(SNIPPETS[cls])) == SNIPPETS[cls]


def _snippet_of_length(n: int) -> str:
    """An ASCII fmxmlsnippet exactly n characters (and bytes) long."""
    shell = '<fmxmlsnippet type="FMObjectList"></fmxmlsnippet>'
    return shell.replace('><', '>' + ' ' * (n - len(shell)) + '<')


def test_captured_prefix_bytes():
    # FMCuttingBoard's Mac-XMSS capture: 131 bytes, starting 7F 00 00 00 '<fmxmlsnippet'
    text = _snippet_of_length(127)
    assert _prefixed(text)[:4] == bytes.fromhex('7F000000')
    assert cw.decode_payload('XMSS', _prefixed(text)) == text


def test_bytes_after_the_payload_are_ignored():
    # GlobalSize can report a block larger than what was written
    assert cw.decode_payload('XMSS', _prefixed(STEPS) + b'\x00' * 12) == STEPS


def test_prefix_whose_first_byte_is_angle_bracket():
    text = _snippet_of_length(0x3C)
    data = _prefixed(text)
    assert data[:1] == b'<'
    assert cw.decode_payload('XMSS', data) == text


def test_unprefixed_payload_is_read_whole():
    assert cw.decode_payload('XMSS', STEPS.encode('utf-8') + b'\x00') == STEPS


def test_truncated_payload_is_an_error():
    with pytest.raises(cw.ClipboardError, match='truncated'):
        cw.decode_payload('XMSS', _prefixed(STEPS)[:-5])


def test_short_or_foreign_payload_is_an_error():
    with pytest.raises(cw.ClipboardError):
        cw.decode_payload('XMSS', b'\x01\x02')
    with pytest.raises(cw.ClipboardError, match='fmxmlsnippet'):
        cw.decode_payload('XMSS', b'\x02\x00\x00\x00ab')


def test_menu_decoding():
    assert cw.decode_payload('ut16', MENU.encode('utf-16') + b'\x00\x00') == MENU


# ---------------------------------------------------------------------------
# Class detection, names and input files
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('cls', sorted(SNIPPETS))
def test_detect_class_from_xml(cls):
    assert cw.detect_class_from_xml(SNIPPETS[cls]) == cls


def test_menu_detected_before_its_steps():
    assert cw.detect_class_from_xml(MENU) == 'ut16'
    assert cw.detect_class_from_xml('<broken><CustomMenu id="1"><Step id="1"') == 'ut16'


def test_format_names():
    assert cw.win_format_name('XMSS') == 'Mac-XMSS'
    assert cw.class_from_format_name('Mac-XML2') == 'XML2'
    assert cw.class_from_format_name('Mac-ut16') == 'ut16'
    assert cw.class_from_format_name('Mac-XXXX') is None
    assert cw.class_from_format_name('CF_UNICODETEXT') is None


def test_normalize_class():
    assert cw.normalize_class('xmsc') == 'XMSC'
    assert cw.normalize_class('UT16') == 'ut16'
    with pytest.raises(cw.ClipboardError, match='Unknown class'):
        cw.normalize_class('XMZZ')


@pytest.mark.parametrize('encoding', ['utf-8', 'utf-8-sig', 'utf-16'])
def test_decode_file(encoding):
    assert cw.decode_file(STEPS.encode(encoding)) == STEPS


def test_clipboard_calls_need_windows(monkeypatch):
    monkeypatch.setattr(cw, '_win32', None)
    monkeypatch.setattr(sys, 'platform', 'darwin')
    with pytest.raises(cw.ClipboardError, match='needs Windows'):
        cw.detect_clipboard_class()


# ---------------------------------------------------------------------------
# Win32 round trips (Windows only; they overwrite the clipboard)
# ---------------------------------------------------------------------------

windows_only = pytest.mark.skipif(sys.platform != 'win32', reason='Win32 clipboard')


@windows_only
@pytest.mark.parametrize('cls', sorted(SNIPPETS) + ['ut16'])
def test_write_then_read(tmp_path, cls):
    text = MENU if cls == 'ut16' else SNIPPETS[cls]
    src = tmp_path / 'in.xml'
    src.write_bytes(text.encode('utf-8'))

    cw.write_to_clipboard(str(src))

    assert cw.detect_clipboard_class() == cls
    found, raw = cw.get_fm_clipboard()
    assert found == cls
    assert raw.startswith(cw.encode_payload(cls, text))
    assert cw.decode_payload(cls, raw) == text


@windows_only
def test_other_formats_are_skipped(tmp_path):
    cw.set_clipboard('agentic-fm test format', b'not FileMaker')
    assert cw.detect_clipboard_class() is None
    assert cw.get_fm_clipboard() == (None, None)
    with pytest.raises(cw.ClipboardError, match='No FileMaker objects'):
        cw.read_from_clipboard()


@windows_only
def test_cli_write_then_read(tmp_path):
    src = tmp_path / 'in.xml'
    out = tmp_path / 'out.xml'
    src.write_text(STEPS, encoding='utf-8')

    write = subprocess.run([sys.executable, str(SCRIPT), 'write', str(src), '--class', 'xmss'],
                           capture_output=True, text=True, encoding='utf-8')
    assert write.returncode == 0, write.stderr
    assert 'Mac-XMSS' in write.stderr

    read = subprocess.run([sys.executable, str(SCRIPT), 'read', str(out)],
                          capture_output=True, text=True, encoding='utf-8')
    assert read.returncode == 0, read.stderr
    # xmllint may reformat the XML; the content survives either way
    assert 'a ≠ b ¶ café' in out.read_text(encoding='utf-8')


@windows_only
def test_cli_rejects_unknown_class(tmp_path):
    src = tmp_path / 'in.xml'
    src.write_text(STEPS, encoding='utf-8')
    result = subprocess.run([sys.executable, str(SCRIPT), 'write', str(src), '--class', 'XMZZ'],
                            capture_output=True, text=True, encoding='utf-8')
    assert result.returncode == 1
    assert 'Unknown class' in result.stderr
