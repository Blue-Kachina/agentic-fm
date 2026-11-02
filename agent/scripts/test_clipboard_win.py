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
# Shaped like FileMaker 26's copies from Manage Custom Menus (CF_UNICODETEXT, captured 2026-11-02)
MENU = ('<FMObjectTransfer membercount="1" version="1" Source="26.0.3"><CustomMenuCatalog membercount="1">'
        '<ObjectList membercount="1"><CustomMenu name="M" id="1"><MenuItemList membercount="1"><MenuItem>'
        '<Step id="89" name="# (comment)"></Step><Override><Title><Calculation><Text><![CDATA["a ≠ b café"]]>'
        '</Text></Calculation></Title></Override></MenuItem></MenuItemList></CustomMenu></ObjectList>'
        '</CustomMenuCatalog><PasteIndexList membercount="1"><Object id="1"></Object></PasteIndexList></FMObjectTransfer>')
MENU_SET = ('<FMObjectTransfer membercount="1" version="1" Source="26.0.3"><CustomMenuSetCatalog membercount="1">'
            '<ObjectList membercount="1"><CustomMenuSet name="S" id="2" comment=""><CustomMenuList membercount="1">'
            '<CustomMenuReference name="M" id="1"></CustomMenuReference></CustomMenuList></CustomMenuSet></ObjectList>'
            '</CustomMenuSetCatalog><PasteIndexList membercount="1"><Object id="2"></Object></PasteIndexList>'
            '</FMObjectTransfer>')
UTF16_DECLARATION = '<?xml version="1.0" encoding="utf-16"?>'


def _menu_text(xml: str) -> bytes:
    """CF_UNICODETEXT bytes as FileMaker writes them."""
    return (UTF16_DECLARATION + xml).encode('utf-16-le') + b'\x00\x00'

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


@pytest.mark.parametrize('xml', [MENU, MENU_SET])
def test_menu_payload_is_filemakers_unicode_text(xml):
    # Any declaration in the file is replaced by FileMaker's own utf-16 one
    for source in (xml, '<?xml version="1.0" encoding="UTF-8"?>\n' + xml, UTF16_DECLARATION + xml):
        data = cw.encode_payload('ut16', source)
        assert data == _menu_text(xml)
        assert data[:2] == b'<\x00'  # no BOM
        assert data.endswith(b'\x00\x00')


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


@pytest.mark.parametrize('xml', [MENU, MENU_SET])
def test_menu_decoding_drops_the_utf16_declaration(xml):
    # The XML is saved as UTF-8, so a declaration saying utf-16 would be wrong
    assert cw.decode_payload('ut16', _menu_text(xml)) == xml
    # Bytes after the NUL terminator (GlobalSize rounding) and a BOM are ignored
    assert cw.decode_payload('ut16', b'\xff\xfe' + _menu_text(xml) + b'\x00\x00\x41\x00') == xml


def test_menu_text_recognised():
    assert cw.is_menu_text(UTF16_DECLARATION + MENU)
    assert cw.is_menu_text(MENU_SET)
    assert cw.is_menu_text(cw.decode_payload('ut16', _menu_text(MENU)))


@pytest.mark.parametrize('text', [
    '',
    'hello',
    'a ≠ b',
    STEPS,
    '<FMObjectTransfer><BaseTableCatalog></BaseTableCatalog></FMObjectTransfer>',
    '<FMObjectTransfer><CustomMenuCatalog>',  # cut short
    '<Other><CustomMenuCatalog></CustomMenuCatalog></Other>',
])
def test_other_text_is_not_a_menu(text):
    assert not cw.is_menu_text(text)


# ---------------------------------------------------------------------------
# Class detection, names and input files
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('cls', sorted(SNIPPETS))
def test_detect_class_from_xml(cls):
    assert cw.detect_class_from_xml(SNIPPETS[cls]) == cls


def test_menu_detected_before_its_steps():
    assert cw.detect_class_from_xml(MENU) == 'ut16'
    assert cw.detect_class_from_xml(MENU_SET) == 'ut16'
    assert cw.detect_class_from_xml('<broken><CustomMenu id="1"><Step id="1"') == 'ut16'


def test_format_names():
    assert cw.win_format_name('XMSS') == 'Mac-XMSS'
    assert cw.win_format_name('ut16') == 'CF_UNICODETEXT'
    assert cw.class_from_format_name('Mac-XML2') == 'XML2'
    assert cw.class_from_format_name('Mac-ut16') is None  # FileMaker never uses it
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


ROUND_TRIPS = [(cls, SNIPPETS[cls]) for cls in sorted(SNIPPETS)] + [('ut16', MENU), ('ut16', MENU_SET)]


@windows_only
@pytest.mark.parametrize('cls, text', ROUND_TRIPS, ids=sorted(SNIPPETS) + ['menu', 'menu-set'])
def test_write_then_read(tmp_path, cls, text):
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
@pytest.mark.parametrize('text', ['hello', STEPS, UTF16_DECLARATION + '<FMObjectTransfer></FMObjectTransfer>'])
def test_plain_text_is_not_filemaker(text):
    cw.set_clipboard(cw.CF_UNICODETEXT, text.encode('utf-16-le') + b'\x00\x00')
    assert cw.get_fm_clipboard() == (None, None)


@windows_only
def test_menu_goes_on_the_clipboard_as_unicode_text(tmp_path):
    src = tmp_path / 'menu.xml'
    src.write_text(MENU, encoding='utf-8')
    cw.write_to_clipboard(str(src))
    with cw._OpenClipboard():
        names = {fmt_id: name for fmt_id, name in cw._list_formats()}
        raw = cw._get_bytes(cw.CF_UNICODETEXT)
    assert cw.CF_UNICODETEXT in names
    assert not any(name and name.startswith('Mac-') for name in names.values())
    assert raw.startswith(_menu_text(MENU))


@windows_only
def test_cli_reads_a_filemaker_menu_copy(tmp_path):
    out = tmp_path / 'menu.xml'
    cw.set_clipboard(cw.CF_UNICODETEXT, _menu_text(MENU))
    read = subprocess.run([sys.executable, str(SCRIPT), 'read', str(out)],
                          capture_output=True, text=True, encoding='utf-8')
    assert read.returncode == 0, read.stderr
    saved = out.read_text(encoding='utf-8')
    assert 'encoding="utf-16"' not in saved
    assert 'a ≠ b café' in saved

    detect = subprocess.run([sys.executable, str(SCRIPT), 'detect'],
                            capture_output=True, text=True, encoding='utf-8')
    assert 'CF_UNICODETEXT' in detect.stdout and 'custom menu' in detect.stdout


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
