#!/usr/bin/env python3
"""
clipboard_win.py -- Read and write FileMaker fmxmlsnippets via the Windows clipboard from WSL2.

On Windows, FileMaker Pro registers custom clipboard formats using the "Mac-" prefix
convention from Apple's Carbon cross-platform clipboard era (e.g. "Mac-XMSS" for
script steps, "Mac-XMSC" for full scripts).

The binary payload format is:
  [4 bytes] little-endian uint32 = byte-length of the following XML
  [N bytes] UTF-8 XML starting with <fmxmlsnippet> (no XML declaration)

This differs from macOS where the payload is plain UTF-8 XML with no prefix.

This script runs on WSL2 and delegates clipboard operations to powershell.exe, which
can call the Windows clipboard API (user32.dll / kernel32.dll) via P/Invoke.  The
PowerShell script is written to the Windows TEMP directory so that powershell.exe can
read it (WSL2 filesystem paths are not directly accessible to Windows executables
without the \\\\wsl.localhost\\ UNC form).

Usage:
    Write XML file to clipboard (class auto-detected from XML content):
        python3 agent/scripts/clipboard_win.py write agent/sandbox/myscript.xml

    Write with an explicit FM class override:
        python3 agent/scripts/clipboard_win.py write agent/sandbox/myscript.xml --class XMSS

    Detect what clipboard formats are present -- copy something in FM first, then run:
        python3 agent/scripts/clipboard_win.py detect

    Read FM objects from clipboard, output as XML:
        python3 agent/scripts/clipboard_win.py read [output.xml]

Troubleshooting:
    If FileMaker does not accept the paste, copy a script step from FM Pro, run
    "detect" to see the exact format name FileMaker registers, then pass it
    explicitly via --format-name on the write command.
"""

import argparse
import base64
import os
import re
import struct
import subprocess
import sys
import tempfile
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
}

# Default Windows clipboard format name prefix.
# FileMaker on Windows uses "Mac-XMSS", "Mac-XMSC", etc. following the convention
# Apple introduced with the Carbon cross-platform clipboard layer (FM7+).
DEFAULT_WIN_PREFIX = 'Mac-'


def _win_format_name(cls, prefix=DEFAULT_WIN_PREFIX):
    return f'{prefix}{cls}'


# ---------------------------------------------------------------------------
# PowerShell Win32 type definition (shared across all PS scripts)
# Embedded as a plain string -- no Python f-string interpolation here.
# The here-string closing "@" must be at column 0 with no leading whitespace.
# ---------------------------------------------------------------------------

_PS_TYPE_DEF = '''\
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
using System.Text;
public static class FmClip {
    [DllImport("user32.dll", SetLastError=true)]
    public static extern bool OpenClipboard(IntPtr hWnd);
    [DllImport("user32.dll", SetLastError=true)]
    public static extern bool CloseClipboard();
    [DllImport("user32.dll", SetLastError=true)]
    public static extern bool EmptyClipboard();
    [DllImport("user32.dll", SetLastError=true, CharSet=CharSet.Unicode)]
    public static extern uint RegisterClipboardFormat(string name);
    [DllImport("kernel32.dll", SetLastError=true)]
    public static extern IntPtr GlobalAlloc(uint flags, UIntPtr size);
    [DllImport("kernel32.dll", SetLastError=true)]
    public static extern IntPtr GlobalLock(IntPtr hMem);
    [DllImport("kernel32.dll", SetLastError=true)]
    public static extern bool GlobalUnlock(IntPtr hMem);
    [DllImport("user32.dll", SetLastError=true)]
    public static extern IntPtr SetClipboardData(uint format, IntPtr hMem);
    [DllImport("user32.dll", SetLastError=true)]
    public static extern IntPtr GetClipboardData(uint format);
    [DllImport("kernel32.dll", SetLastError=true)]
    public static extern UIntPtr GlobalSize(IntPtr hMem);
    [DllImport("user32.dll", SetLastError=true)]
    public static extern uint EnumClipboardFormats(uint format);
    [DllImport("user32.dll", SetLastError=true, CharSet=CharSet.Unicode)]
    public static extern int GetClipboardFormatName(uint format, StringBuilder buf, int max);
}
"@

'''

# ---------------------------------------------------------------------------
# Environment / PowerShell helpers
# ---------------------------------------------------------------------------

_ps_exe_cache = None
_win_temp_cache = None


def _require_powershell():
    global _ps_exe_cache
    if _ps_exe_cache:
        return _ps_exe_cache
    for candidate in (
        'powershell.exe',
        '/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe',
    ):
        try:
            r = subprocess.run(
                [candidate, '-NoProfile', '-Command', 'echo ok'],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode == 0:
                _ps_exe_cache = candidate
                return candidate
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    print('ERROR: powershell.exe not found. This script requires WSL2 on Windows.', file=sys.stderr)
    sys.exit(1)


def _get_win_temp() -> tuple:
    """Return (wsl_path, windows_path) for the Windows TEMP directory."""
    global _win_temp_cache
    if _win_temp_cache:
        return _win_temp_cache
    ps = _require_powershell()
    r = subprocess.run(
        [ps, '-NoProfile', '-Command', '[System.IO.Path]::GetTempPath()'],
        capture_output=True, text=True, timeout=10,
    )
    if r.returncode != 0 or not r.stdout.strip():
        _win_temp_cache = ('/tmp', r'C:\Windows\Temp')
        return _win_temp_cache
    win_path = r.stdout.strip().rstrip('\\')
    r2 = subprocess.run(['wslpath', win_path], capture_output=True, text=True)
    wsl_path = r2.stdout.strip() if r2.returncode == 0 else '/tmp'
    _win_temp_cache = (wsl_path, win_path)
    return _win_temp_cache


def _run_ps1(script_body: str) -> subprocess.CompletedProcess:
    """Write script_body to a .ps1 file in Windows TEMP and execute it via powershell.exe."""
    ps = _require_powershell()
    wsl_temp, _win_temp = _get_win_temp()

    fd, wsl_ps_path = tempfile.mkstemp(suffix='.ps1', dir=wsl_temp, prefix='fm_clip_')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(script_body)

        r = subprocess.run(['wslpath', '-w', wsl_ps_path], capture_output=True, text=True)
        if r.returncode != 0:
            print(f'ERROR: wslpath -w failed: {r.stderr.strip()}', file=sys.stderr)
            sys.exit(1)
        win_ps_path = r.stdout.strip()

        return subprocess.run(
            [
                ps, '-NoProfile', '-NonInteractive',
                '-ExecutionPolicy', 'Bypass',
                '-File', win_ps_path,
            ],
            capture_output=True, text=True, timeout=30,
        )
    finally:
        try:
            os.unlink(wsl_ps_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# XML class detection (mirrors clipboard.py)
# ---------------------------------------------------------------------------

def detect_class_from_xml(xml_text: str) -> str:
    try:
        root = ET.fromstring(xml_text)
        if len(root) > 0:
            cls = XML_ELEMENT_TO_CLASS.get(root[0].tag)
            if cls:
                return cls
    except ET.ParseError:
        pass
    for element in ('CustomMenuSet', 'CustomMenu'):
        if re.search(rf'<{element}[\s>/]', xml_text):
            return 'ut16'
    for element, cls in XML_ELEMENT_TO_CLASS.items():
        if element in ('CustomMenuSet', 'CustomMenu'):
            continue
        if re.search(rf'<{element}[\s>/]', xml_text):
            return cls
    return 'XMSS'


# ---------------------------------------------------------------------------
# write
# ---------------------------------------------------------------------------

def write_to_clipboard(input_path: str, cls: str = None, prefix: str = DEFAULT_WIN_PREFIX):
    """Write an fmxmlsnippet XML file to the Windows clipboard as FM objects."""
    with open(input_path, 'rb') as f:
        raw_bytes = f.read()

    if raw_bytes[:2] in (b'\xff\xfe', b'\xfe\xff'):
        xml_text = raw_bytes.decode('utf-16')
        raw_bytes = xml_text.encode('utf-8')
    else:
        xml_text = raw_bytes.decode('utf-8', errors='replace')

    if cls is None:
        cls = detect_class_from_xml(xml_text)
    cls = cls.lower() if cls.lower() == 'ut16' else cls.upper()

    if cls == 'ut16':
        xml_text = re.sub(r'<\?xml[^?]*\?>\s*', '', xml_text, count=1)
        raw_bytes = xml_text.encode('utf-16')
    else:
        # Strip XML declaration — FM on Windows stores raw XML without it
        xml_clean = re.sub(r'<\?xml[^?]*\?>\s*', '', xml_text, count=1)
        xml_bytes = xml_clean.encode('utf-8')
        # Windows FM clipboard format: 4-byte little-endian length prefix + UTF-8 XML
        raw_bytes = struct.pack('<I', len(xml_bytes)) + xml_bytes

    if cls not in FM_CLASSES and cls != 'ut16':
        print(f"ERROR: Unknown class '{cls}'. Valid: {', '.join(FM_CLASSES)}", file=sys.stderr)
        sys.exit(1)

    format_name = _win_format_name(cls, prefix)
    # base64 alphabet contains only A-Z a-z 0-9 + / = -- safe in single-quoted PS string
    b64 = base64.b64encode(raw_bytes).decode('ascii')

    # Build the PowerShell script.
    # _PS_TYPE_DEF is NOT an f-string so its { } are literal.
    # The f-string below uses {{ }} for literal PS braces.
    ps = _PS_TYPE_DEF + f"""\
$formatName = '{format_name}'
$bytes = [Convert]::FromBase64String('{b64}')

$fmt = [FmClip]::RegisterClipboardFormat($formatName)
if ($fmt -eq 0) {{
    $err = [System.Runtime.InteropServices.Marshal]::GetLastWin32Error()
    Write-Error "RegisterClipboardFormat failed (Win32 error $err)"
    exit 1
}}

if (-not [FmClip]::OpenClipboard([IntPtr]::Zero)) {{
    $err = [System.Runtime.InteropServices.Marshal]::GetLastWin32Error()
    Write-Error "OpenClipboard failed (Win32 error $err)"
    exit 1
}}

try {{
    [FmClip]::EmptyClipboard() | Out-Null
    $hMem = [FmClip]::GlobalAlloc([uint32]0x0002, [UIntPtr][uint64]$bytes.Length)
    if ($hMem -eq [IntPtr]::Zero) {{
        Write-Error "GlobalAlloc failed"
        exit 1
    }}
    $ptr = [FmClip]::GlobalLock($hMem)
    [System.Runtime.InteropServices.Marshal]::Copy($bytes, 0, $ptr, $bytes.Length)
    [FmClip]::GlobalUnlock($hMem) | Out-Null
    $r = [FmClip]::SetClipboardData($fmt, $hMem)
    if ($r -eq [IntPtr]::Zero) {{
        $err = [System.Runtime.InteropServices.Marshal]::GetLastWin32Error()
        Write-Error "SetClipboardData failed (Win32 error $err)"
        exit 1
    }}
    Write-Output 'OK'
}} finally {{
    [FmClip]::CloseClipboard() | Out-Null
}}
"""

    result = _run_ps1(ps)
    if result.returncode != 0 or 'OK' not in result.stdout:
        err = result.stderr.strip() or result.stdout.strip() or 'unknown error'
        print(f'ERROR: Clipboard write failed:\n{err}', file=sys.stderr)
        sys.exit(1)

    label = FM_CLASSES.get(cls, 'Menu')
    print(f'Clipboard ready → {input_path} as {format_name} ({label})', file=sys.stderr)


# ---------------------------------------------------------------------------
# detect
# ---------------------------------------------------------------------------

def detect_clipboard_formats(prefix: str = DEFAULT_WIN_PREFIX):
    """List all custom clipboard formats currently on the Windows clipboard."""
    ps = _PS_TYPE_DEF + """\
if (-not [FmClip]::OpenClipboard([IntPtr]::Zero)) {
    Write-Error 'OpenClipboard failed'
    exit 1
}
try {
    $f = [uint32]0
    $count = 0
    while (($f = [FmClip]::EnumClipboardFormats($f)) -ne [uint32]0) {
        $sb = New-Object System.Text.StringBuilder 256
        $n = [FmClip]::GetClipboardFormatName($f, $sb, 256)
        if ($n -gt 0) {
            Write-Output ('{0,6}  {1}' -f $f, $sb.ToString())
            $count++
        }
    }
    if ($count -eq 0) { Write-Output '(none)' }
} finally {
    [FmClip]::CloseClipboard() | Out-Null
}
"""

    result = _run_ps1(ps)
    if result.returncode != 0:
        print(f'ERROR: {result.stderr.strip()}', file=sys.stderr)
        sys.exit(1)

    lines = result.stdout.strip().splitlines()
    if not lines or lines[0].strip() == '(none)':
        print('No custom clipboard formats found.')
        print('(Clipboard may be empty or contain only built-in formats like CF_TEXT.)')
        return

    fm_names = {_win_format_name(c, prefix) for c in list(FM_CLASSES) + ['ut16']}
    print('Custom clipboard formats on the Windows clipboard:')
    for line in lines:
        print(f'  {line}')
        for name in fm_names:
            if name in line:
                print(f'    ^ FileMaker format: {name}')

    print()
    print('Tip: if the FileMaker format name shown above differs from the default')
    print(f'     prefix "{prefix}", re-run write with: --format-prefix <prefix>')


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------

def read_from_clipboard(output_path: str = None, prefix: str = DEFAULT_WIN_PREFIX):
    """Read FileMaker objects from the Windows clipboard and output as XML."""
    candidates = [_win_format_name(c, prefix) for c in FM_CLASSES] + [_win_format_name('ut16', prefix)]
    candidates_ps = ', '.join(f"'{n}'" for n in candidates)

    ps = _PS_TYPE_DEF + f"""\
$candidates = @({candidates_ps})

if (-not [FmClip]::OpenClipboard([IntPtr]::Zero)) {{
    Write-Error 'OpenClipboard failed'
    exit 1
}}
try {{
    foreach ($name in $candidates) {{
        $fmt = [FmClip]::RegisterClipboardFormat($name)
        $hData = [FmClip]::GetClipboardData($fmt)
        if ($hData -ne [IntPtr]::Zero) {{
            $ptr = [FmClip]::GlobalLock($hData)
            $sz = [uint64][FmClip]::GlobalSize($hData)
            $bytes = New-Object byte[] $sz
            [System.Runtime.InteropServices.Marshal]::Copy($ptr, $bytes, 0, [int]$sz)
            [FmClip]::GlobalUnlock($hData) | Out-Null
            Write-Output "FORMAT:$name"
            Write-Output ([Convert]::ToBase64String($bytes))
            break
        }}
    }}
}} finally {{
    [FmClip]::CloseClipboard() | Out-Null
}}
"""

    result = _run_ps1(ps)
    if result.returncode != 0:
        print(f'ERROR: {result.stderr.strip()}', file=sys.stderr)
        sys.exit(1)

    lines = result.stdout.strip().splitlines()
    if not lines or not lines[0].startswith('FORMAT:'):
        print('ERROR: No FileMaker objects found on clipboard.', file=sys.stderr)
        sys.exit(1)

    format_name = lines[0][len('FORMAT:'):]
    b64 = ''.join(lines[1:])
    raw_bytes = base64.b64decode(b64)

    if 'ut16' in format_name:
        xml_text = raw_bytes.decode('utf-16')
    else:
        # Strip 4-byte little-endian length prefix written by FM on Windows
        if len(raw_bytes) >= 4:
            length = struct.unpack('<I', raw_bytes[:4])[0]
            if length == len(raw_bytes) - 4:
                raw_bytes = raw_bytes[4:]
        xml_text = raw_bytes.decode('utf-8')

    try:
        fmt_result = subprocess.run(
            ['xmllint', '--format', '-'],
            input=xml_text.encode('utf-8'), capture_output=True,
        )
        if fmt_result.returncode == 0:
            xml_text = fmt_result.stdout.decode('utf-8')
    except FileNotFoundError:
        pass

    if output_path:
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(xml_text)
        print(f'Saved {format_name} to {output_path}', file=sys.stderr)
    else:
        print(xml_text)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='Read/write FileMaker fmxmlsnippets via the Windows clipboard from WSL2',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            'FM class codes: '
            + ', '.join(f'{k}={v}' for k, v in FM_CLASSES.items())
            + '\n\n'
            'If FileMaker does not accept the paste, copy a script step from FM Pro, then\n'
            'run "detect" to see the exact format name FM registers on this installation.\n'
            'Pass the observed prefix via --format-prefix on the write command.'
        ),
    )
    sub = parser.add_subparsers(dest='cmd')

    wp = sub.add_parser('write', help='Write XML to Windows clipboard as FM objects')
    wp.add_argument('input', help='fmxmlsnippet XML file')
    wp.add_argument('--class', dest='cls', default=None,
                    help='FM class override (default: auto-detect from XML). Use "ut16" for menus.')
    wp.add_argument('--format-prefix', dest='prefix', default=DEFAULT_WIN_PREFIX,
                    help=f'Windows format name prefix (default: "{DEFAULT_WIN_PREFIX}"). '
                         'Use "" (empty) to try bare class codes like "XMSS".')

    rp = sub.add_parser('read', help='Read FM objects from Windows clipboard as XML')
    rp.add_argument('output', nargs='?', help='Output file (default: stdout)')
    rp.add_argument('--format-prefix', dest='prefix', default=DEFAULT_WIN_PREFIX,
                    help=f'Windows format name prefix (default: "{DEFAULT_WIN_PREFIX}").')

    dp = sub.add_parser('detect', help='List custom clipboard formats (copy from FM first)')
    dp.add_argument('--format-prefix', dest='prefix', default=DEFAULT_WIN_PREFIX,
                    help=f'Prefix used to tag FileMaker formats in output (default: "{DEFAULT_WIN_PREFIX}").')

    args = parser.parse_args()

    # Harden against PowerShell-string injection: the format prefix is
    # interpolated into a single-quoted PS string, so restrict it to a safe
    # charset — a stray "'" could otherwise break out of the string.
    prefix = getattr(args, 'prefix', None)
    if prefix is not None and not re.fullmatch(r'[A-Za-z0-9_-]*', prefix):
        print("ERROR: --format-prefix may only contain letters, digits, '-' and '_'.",
              file=sys.stderr)
        sys.exit(1)

    if args.cmd == 'write':
        write_to_clipboard(args.input, args.cls, args.prefix)
    elif args.cmd == 'read':
        read_from_clipboard(args.output, args.prefix)
    elif args.cmd == 'detect':
        detect_clipboard_formats(args.prefix)
    else:
        parser.print_help()


if __name__ == '__main__':
    # Windows pipes and redirects default to the ANSI code page: accented names would garble
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8', errors='backslashreplace')
    main()
