#!/usr/bin/env python3
"""
clipboard_win.py -- Read and write FileMaker fmxmlsnippets via the native Windows clipboard.

On Windows, FileMaker Pro registers custom clipboard formats using the "Mac-" prefix
convention from Apple's Carbon cross-platform clipboard era (e.g. "Mac-XMSS" for
script steps, "Mac-XMSC" for full scripts).

The payload of each format is (spec: agent/docs/CLIPBOARD_FORMATS.md):
  [4 bytes] uint32 little-endian = N
  [N bytes] UTF-8 XML, no BOM, LF line endings, no trailing NUL

This differs from macOS where the payload is plain UTF-8 XML with no prefix.

Custom menus and menu sets are the exception: FileMaker copies them as plain Unicode
text (CF_UNICODETEXT: UTF-16LE, no BOM, NUL-terminated), an <FMObjectTransfer> document
declared encoding="utf-16". They keep the class code 'ut16', as on macOS.

The clipboard is reached through user32/kernel32 with ctypes (standard library only).
The encoding functions are pure and import on any OS; the clipboard calls need Windows.

Usage:
    Read FM objects from clipboard, print XML to stdout:
        python agent/scripts/clipboard_win.py read

    Read FM objects from clipboard, save to file:
        python agent/scripts/clipboard_win.py read agent/sandbox/output.xml

    Write XML file to clipboard (class auto-detected from XML content):
        python agent/scripts/clipboard_win.py write agent/sandbox/myscript.xml

    Write XML file to clipboard with an explicit class override:
        python agent/scripts/clipboard_win.py write agent/sandbox/myscript.xml --class XMSC

    List the formats on the clipboard -- copy something in FM first:
        python agent/scripts/clipboard_win.py detect
"""

import argparse
import ctypes
import re
import struct
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

# ---------------------------------------------------------------------------
# FileMaker class / format name tables (same codes as clipboard.py / macOS)
# ---------------------------------------------------------------------------

FM_CLASSES = {
    'XMSS': 'Script Steps',
    'XMSC': 'Script',
    'XML2': 'Layout Objects',
    'XMLO': 'Layout Objects (legacy)',
    'XMFD': 'Field Definition',
    'XMFN': 'Custom Function',
    'XMTB': 'Table',
    'XMVL': 'Value List',
    'XMTH': 'Theme',
}

XML_ELEMENT_TO_CLASS = {
    'Step':           'XMSS',
    'Script':         'XMSC',
    'CustomFunction': 'XMFN',
    'Field':          'XMFD',
    'BaseTable':      'XMTB',
    'ValueList':      'XMVL',
    'Layout':         'XML2',
    'Theme':          'XMTH',
    'CustomMenu':     'ut16',
    'CustomMenuSet':  'ut16',
    'CustomMenuCatalog':    'ut16',
    'CustomMenuSetCatalog': 'ut16',
}

# Menus travel as plain Unicode text, not as a registered FileMaker format
UT16_CLASSES = {'ut16'}
CF_UNICODETEXT = 13
MENU_CATALOGS = {'CustomMenuCatalog', 'CustomMenuSetCatalog'}
UTF16_DECLARATION = '<?xml version="1.0" encoding="utf-16"?>'

# FileMaker keeps the XML declaration in layout objects and leaves it out everywhere else
DECLARATION_CLASSES = {'XML2', 'XMLO'}
XML_DECLARATION = '<?xml version="1.0" encoding="UTF-8"?>\n'

# FileMaker on Windows uses "Mac-XMSS", "Mac-XMSC", etc. following the convention
# Apple introduced with the Carbon cross-platform clipboard layer (FM7+).
WIN_PREFIX = 'Mac-'

_DECLARATION_RE = re.compile(r'<\?xml[^?]*\?>\s*')
_UTF8_BOM = b'\xef\xbb\xbf'


class ClipboardError(Exception):
    """A clipboard operation or payload that cannot be completed."""


def win_format_name(cls):
    """The clipboard format a class travels in: 'Mac-XMSS', or 'CF_UNICODETEXT' for menus."""
    return 'CF_UNICODETEXT' if cls in UT16_CLASSES else f'{WIN_PREFIX}{cls}'


def class_from_format_name(name):
    """'Mac-XMSS' -> 'XMSS'; None for a format that is not a FileMaker one."""
    if not name.startswith(WIN_PREFIX):
        return None
    cls = name[len(WIN_PREFIX):]
    return cls if cls in FM_CLASSES else None


def is_menu_text(text):
    """True when plain clipboard text is a FileMaker custom menu or menu set copy."""
    if '<FMObjectTransfer' not in text[:500]:
        return False
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return False
    return root.tag == 'FMObjectTransfer' and any(child.tag in MENU_CATALOGS for child in root)


# ---------------------------------------------------------------------------
# XML class detection (mirrors clipboard.py)
# ---------------------------------------------------------------------------

def detect_class_from_xml(xml_text):
    """Infer the correct FM clipboard class from the XML element content."""
    try:
        root = ET.fromstring(xml_text)
        if len(root) > 0:
            cls = XML_ELEMENT_TO_CLASS.get(root[0].tag)
            if cls:
                return cls
    except ET.ParseError:
        pass
    # Menu elements must be checked before 'Step': menu XML contains <Step> in its actions
    for element in ('CustomMenuSet', 'CustomMenu'):
        if re.search(rf'<{element}[\s>/]', xml_text):
            return 'ut16'
    for element, cls in XML_ELEMENT_TO_CLASS.items():
        if element in ('CustomMenuSet', 'CustomMenu'):
            continue
        if re.search(rf'<{element}[\s>/]', xml_text):
            return cls
    return 'XMSS'  # safe default for steps-only snippets


def normalize_class(cls):
    """Case-fold a class code from the command line; raise for an unknown one."""
    cls = cls.lower() if cls.lower() in UT16_CLASSES else cls.upper()
    if cls not in FM_CLASSES and cls not in UT16_CLASSES:
        valid = ', '.join(list(FM_CLASSES) + sorted(UT16_CLASSES))
        raise ClipboardError(f"Unknown class '{cls}'. Valid: {valid}")
    return cls


# ---------------------------------------------------------------------------
# Payload encoding (pure; no clipboard access)
# ---------------------------------------------------------------------------

def decode_file(raw_bytes):
    """Decode XML file bytes, honouring a UTF-8 or UTF-16 BOM."""
    if raw_bytes[:2] in (b'\xff\xfe', b'\xfe\xff'):
        return raw_bytes.decode('utf-16')
    return raw_bytes.decode('utf-8-sig')


def encode_payload(cls, xml_text):
    """Build the clipboard bytes for one FileMaker format from XML text."""
    xml_text = _DECLARATION_RE.sub('', xml_text.lstrip('﻿'), count=1)
    xml_text = xml_text.replace('\r\n', '\n').replace('\r', '\n')
    if cls in UT16_CLASSES:
        # As FileMaker writes it: UTF-16LE, no BOM, its own declaration, a two-byte NUL
        return (UTF16_DECLARATION + xml_text).encode('utf-16-le') + b'\x00\x00'
    if cls in DECLARATION_CLASSES:
        xml_text = XML_DECLARATION + xml_text
    body = xml_text.encode('utf-8')
    return struct.pack('<I', len(body)) + body


def decode_payload(cls, raw_bytes):
    """Return the XML text held in one FileMaker format's clipboard bytes.

    The length prefix decides how many bytes are XML: the memory block can be
    larger than what was written. A payload with no prefix (XML from its first
    byte, as tools following the macOS layout write it) is read whole.
    """
    if cls in UT16_CLASSES:
        # CF_UNICODETEXT is UTF-16LE; tolerate a BOM, drop the NUL terminator. The
        # encoding="utf-16" declaration goes too: the text is saved as UTF-8
        text = raw_bytes.decode('utf-16-le').lstrip('﻿').split('\x00', 1)[0]
        return _DECLARATION_RE.sub('', text, count=1)
    name = win_format_name(cls)
    if len(raw_bytes) >= 4:
        length = struct.unpack('<I', raw_bytes[:4])[0]
        body = raw_bytes[4:4 + length]
        # The prefix's low byte can itself be '<' (0x3C), so try the prefixed reading first
        if len(body) == length and (body[:1] == b'<' or body[:3] == _UTF8_BOM):
            return body.decode('utf-8-sig')
    if raw_bytes[:1] == b'<' or raw_bytes[:3] == _UTF8_BOM:
        return raw_bytes.decode('utf-8-sig').rstrip('\x00')
    if len(raw_bytes) < 4:
        raise ClipboardError(f'{name} holds {len(raw_bytes)} bytes, too few for a length prefix.')
    if len(raw_bytes) - 4 < length:
        raise ClipboardError(
            f'{name} is truncated: the prefix says {length} bytes, {len(raw_bytes) - 4} follow.'
        )
    raise ClipboardError(f'{name} does not hold an fmxmlsnippet (starts {raw_bytes[:8].hex(" ")}).')


# ---------------------------------------------------------------------------
# Win32 clipboard (ctypes)
# ---------------------------------------------------------------------------

GMEM_MOVEABLE = 0x0002
_OPEN_RETRIES = 20
_OPEN_RETRY_DELAY = 0.05  # seconds; another process may hold the clipboard briefly

_win32 = None


def _api():
    """Load user32/kernel32 with typed signatures (once). Windows only."""
    global _win32
    if _win32 is not None:
        return _win32
    if sys.platform != 'win32':
        raise ClipboardError('clipboard_win.py needs Windows; on macOS use clipboard.py.')
    from ctypes import wintypes as w

    user32 = ctypes.WinDLL('user32', use_last_error=True)
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)

    def sig(fn, restype, *argtypes):
        fn.restype = restype
        fn.argtypes = list(argtypes)

    sig(user32.OpenClipboard, w.BOOL, w.HWND)
    sig(user32.CloseClipboard, w.BOOL)
    sig(user32.EmptyClipboard, w.BOOL)
    sig(user32.RegisterClipboardFormatW, w.UINT, w.LPCWSTR)
    sig(user32.EnumClipboardFormats, w.UINT, w.UINT)
    sig(user32.GetClipboardFormatNameW, ctypes.c_int, w.UINT, w.LPWSTR, ctypes.c_int)
    sig(user32.GetClipboardData, w.HANDLE, w.UINT)
    sig(user32.SetClipboardData, w.HANDLE, w.UINT, w.HANDLE)
    sig(kernel32.GlobalAlloc, w.HGLOBAL, w.UINT, ctypes.c_size_t)
    sig(kernel32.GlobalFree, w.HGLOBAL, w.HGLOBAL)
    sig(kernel32.GlobalLock, ctypes.c_void_p, w.HGLOBAL)
    sig(kernel32.GlobalUnlock, w.BOOL, w.HGLOBAL)
    sig(kernel32.GlobalSize, ctypes.c_size_t, w.HGLOBAL)
    _win32 = (user32, kernel32)
    return _win32


def _last_error(what):
    return ClipboardError(f'{what} failed (Win32 error {ctypes.get_last_error()})')


class _OpenClipboard:
    """Context manager: open the clipboard, retrying while another process holds it."""

    def __enter__(self):
        user32, _ = _api()
        for _ in range(_OPEN_RETRIES):
            if user32.OpenClipboard(None):
                return self
            time.sleep(_OPEN_RETRY_DELAY)
        raise _last_error('OpenClipboard (the clipboard is busy)')

    def __exit__(self, *exc):
        _api()[0].CloseClipboard()
        return False


def _format_name(fmt_id):
    user32, _ = _api()
    buf = ctypes.create_unicode_buffer(256)
    return buf.value if user32.GetClipboardFormatNameW(fmt_id, buf, len(buf)) else None


def _list_formats():
    """[(id, name or None)] in the order the clipboard owner offered them. Clipboard must be open."""
    user32, _ = _api()
    formats = []
    fmt_id = 0
    while True:
        fmt_id = user32.EnumClipboardFormats(fmt_id)
        if not fmt_id:
            return formats
        formats.append((fmt_id, _format_name(fmt_id)))


def _get_bytes(fmt_id):
    """Raw bytes of one format, or None. Clipboard must be open."""
    user32, kernel32 = _api()
    handle = user32.GetClipboardData(fmt_id)
    if not handle:
        return None
    ptr = kernel32.GlobalLock(handle)
    if not ptr:
        raise _last_error('GlobalLock')
    try:
        return ctypes.string_at(ptr, kernel32.GlobalSize(handle))
    finally:
        kernel32.GlobalUnlock(handle)


def set_clipboard(fmt, data):
    """Replace the clipboard's contents with one format holding data.

    fmt is a custom format name (registered here) or a standard format id such as CF_UNICODETEXT.
    """
    user32, kernel32 = _api()
    fmt_id = fmt if isinstance(fmt, int) else user32.RegisterClipboardFormatW(fmt)
    if not fmt_id:
        raise _last_error(f'RegisterClipboardFormat({fmt})')
    with _OpenClipboard():
        if not user32.EmptyClipboard():
            raise _last_error('EmptyClipboard')
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not handle:
            raise _last_error('GlobalAlloc')
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            kernel32.GlobalFree(handle)
            raise _last_error('GlobalLock')
        ctypes.memmove(ptr, data, len(data))
        kernel32.GlobalUnlock(handle)
        # On success the clipboard owns the memory; on failure it is still ours to free
        if not user32.SetClipboardData(fmt_id, handle):
            err = _last_error('SetClipboardData')
            kernel32.GlobalFree(handle)
            raise err


def _menu_text_bytes():
    """CF_UNICODETEXT's bytes when they hold a FileMaker menu copy, else None. Clipboard must be open."""
    raw = _get_bytes(CF_UNICODETEXT)
    if raw and is_menu_text(raw.decode('utf-16-le', errors='replace').split('\x00', 1)[0]):
        return raw
    return None


def get_fm_clipboard():
    """(class, raw bytes) of the FileMaker objects on the clipboard, or (None, None).

    A 'Mac-' format wins; failing that, plain text holding a menu copy is class 'ut16'.
    """
    with _OpenClipboard():
        formats = _list_formats()
        for fmt_id, name in formats:
            cls = class_from_format_name(name or '')
            if cls:
                return cls, _get_bytes(fmt_id)
        if any(fmt_id == CF_UNICODETEXT for fmt_id, _name in formats):
            raw = _menu_text_bytes()
            if raw:
                return 'ut16', raw
    return None, None


def detect_clipboard_class():
    """Return the FM class code currently on the clipboard, or None."""
    return get_fm_clipboard()[0]


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def _pretty(xml_text):
    """Pretty-print with xmllint when it is installed; otherwise return the XML as is."""
    try:
        result = subprocess.run(['xmllint', '--format', '-'], input=xml_text.encode('utf-8'),
                                capture_output=True)
    except FileNotFoundError:
        return xml_text
    return result.stdout.decode('utf-8') if result.returncode == 0 else xml_text


def read_from_clipboard(output_path=None):
    """Read FM objects from the clipboard and output them as XML."""
    cls, raw_bytes = get_fm_clipboard()
    if not cls:
        raise ClipboardError('No FileMaker objects found on clipboard.')
    if raw_bytes is None:
        raise ClipboardError(f'Could not read {win_format_name(cls)} data from clipboard.')
    xml = _pretty(decode_payload(cls, raw_bytes))

    label = FM_CLASSES.get(cls, 'Menu')
    if output_path:
        with open(output_path, 'w', encoding='utf-8', newline='\n') as f:
            f.write(xml)
        print(f'Saved {cls} ({label}) to {output_path}', file=sys.stderr)
    else:
        print(xml)
    return xml


def write_to_clipboard(input_path, cls=None):
    """Write an fmxmlsnippet XML file to the clipboard as FM objects."""
    with open(input_path, 'rb') as f:
        xml_text = decode_file(f.read())
    cls = normalize_class(cls) if cls else detect_class_from_xml(xml_text)
    format_name = win_format_name(cls)
    fmt = CF_UNICODETEXT if cls in UT16_CLASSES else format_name
    set_clipboard(fmt, encode_payload(cls, xml_text))
    label = FM_CLASSES.get(cls, 'Menu')
    print(f'Clipboard ready → {input_path} as {format_name} ({label})', file=sys.stderr)


def detect_clipboard_formats():
    """List the custom formats on the clipboard, marking FileMaker's."""
    with _OpenClipboard():
        listed = _list_formats()
        formats = [(fmt_id, name, len(_get_bytes(fmt_id) or b'')) for fmt_id, name in listed if name]
        menu = _menu_text_bytes() if any(f == CF_UNICODETEXT for f, _n in listed) else None
    if formats:
        print('Custom clipboard formats on the Windows clipboard:')
        for fmt_id, name, size in formats:
            mark = '  <- FileMaker' if class_from_format_name(name) else ''
            print(f'  {fmt_id:6}  {name}  ({size} bytes){mark}')
    if menu:
        print(f'  {CF_UNICODETEXT:6}  CF_UNICODETEXT  ({len(menu)} bytes)  <- FileMaker custom menu or menu set')
    if not formats and not menu:
        print('No custom clipboard formats found.')
        print('(Clipboard may be empty or contain only built-in formats like CF_UNICODETEXT.)')


def main():
    parser = argparse.ArgumentParser(
        description='Read/write FileMaker fmxmlsnippets via the native Windows clipboard',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f'FM class codes: {", ".join(f"{k}={v}" for k, v in FM_CLASSES.items())}'
    )
    sub = parser.add_subparsers(dest='cmd')

    rp = sub.add_parser('read', help='Read FM objects from clipboard as XML')
    rp.add_argument('output', nargs='?', help='Output file path (default: stdout)')

    wp = sub.add_parser('write', help='Write XML file to clipboard as FM objects')
    wp.add_argument('input', help='fmxmlsnippet XML file to send to clipboard')
    wp.add_argument(
        '--class', dest='cls', default=None,
        help='FM class override (default: auto-detect from XML content). Use "ut16" for menu objects.'
    )

    sub.add_parser('detect', help='List custom clipboard formats (copy from FM first)')

    args = parser.parse_args()

    try:
        if args.cmd == 'read':
            read_from_clipboard(args.output)
        elif args.cmd == 'write':
            write_to_clipboard(args.input, args.cls)
        elif args.cmd == 'detect':
            detect_clipboard_formats()
        else:
            parser.print_help()
    except ClipboardError as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    # Windows pipes and redirects default to the ANSI code page: accented names would garble
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8', errors='backslashreplace')
    main()
