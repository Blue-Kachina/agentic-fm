# Clipboard Interaction

FileMaker does not use plain text for clipboard objects. When you copy scripts, steps, fields, custom functions, or other objects in FileMaker, they are placed on the macOS clipboard as proprietary binary descriptor classes — not as readable text. Converting between those classes and the fmxmlsnippet XML format that this project uses requires AppleScript.

**Do not use `pbpaste` or `pbcopy` for FileMaker objects.** Both tools silently corrupt multi-byte UTF-8 characters (such as `≠`, `≤`, `≥`, `¶`) that are common in FileMaker calculations.

---

## Clipboard class codes

Each FileMaker object type corresponds to a four-letter AppleScript class code:

| Code   | FileMaker object          | fmxmlsnippet element       |
|--------|---------------------------|----------------------------|
| `XMSS` | Script Steps              | `<Step>`                   |
| `XMSC` | Script                    | `<Script>`                 |
| `XML2` | Layout Objects            | `<Layout>` (v12+)          |
| `XMLO` | Layout Objects (legacy)   | `<Layout>`                 |
| `XMFD` | Field Definition          | `<Field>`                  |
| `XMFN` | Custom Function           | `<CustomFunction>`         |
| `XMTB` | Table                     | `<BaseTable>`              |
| `XMVL` | Value List                | `<ValueList>`              |
| `XMTH` | Theme                     | `<Theme>`                  |

This project primarily works with `XMSS` (the output format is steps-only inside `<fmxmlsnippet type="FMObjectList">`).

### CorePasteboardFlavorType values

When accessing the clipboard via the macOS Pasteboard API directly (e.g. via PyObjC's `NSPasteboard`), each class code maps to a `CorePasteboardFlavorType` string. The hex value is simply the four ASCII bytes of the class code interpreted as a big-endian 32-bit integer:

| Code   | Hex value    | Pasteboard type string                         |
|--------|--------------|------------------------------------------------|
| `XMSS` | `0x584D5353` | `CorePasteboardFlavorType 0x584D5353`          |
| `XMSC` | `0x584D5343` | `CorePasteboardFlavorType 0x584D5343`          |
| `XML2` | `0x584D4C32` | `CorePasteboardFlavorType 0x584D4C32`          |
| `XMLO` | `0x584D4C4F` | `CorePasteboardFlavorType 0x584D4C4F`          |
| `XMFD` | `0x584D4644` | `CorePasteboardFlavorType 0x584D4644`          |
| `XMFN` | `0x584D464E` | `CorePasteboardFlavorType 0x584D464E`          |
| `XMTB` | `0x584D5442` | `CorePasteboardFlavorType 0x584D5442`          |
| `XMVL` | `0x584D564C` | `CorePasteboardFlavorType 0x584D564C`          |
| `XMTH` | `0x584D5448` | `CorePasteboardFlavorType 0x584D5448`          |
| `ut16` | `0x75743136` | `CorePasteboardFlavorType 0x75743136`          |

---

## Using clipboard.py

A Python helper script is provided at `agent/scripts/clipboard.py`. It handles both read and write directions and auto-detects the correct class code from the XML content. It is the one clipboard entry point on both platforms: on native Windows it hands every call to `clipboard_win.py` (see [Windows clipboard](#windows-clipboard)), so the commands below are the same there (`python` instead of `python3`, Ctrl+C/Ctrl+V instead of ⌘C/⌘V). Under WSL or Linux it stops with an error.

### Read: FM objects on clipboard → XML file

After copying objects in FileMaker (`⌘C`), run:

```bash
# Print to stdout
python3 agent/scripts/clipboard.py read

# Save directly to the sandbox
python3 agent/scripts/clipboard.py read agent/sandbox/myscript.xml
```

### Write: XML file → FM objects on clipboard

After generating or editing a snippet, send it to the clipboard so it can be pasted into FileMaker (`⌘V`):

```bash
# Class is auto-detected from the XML content
python3 agent/scripts/clipboard.py write agent/sandbox/myscript.xml

# Override the class explicitly if needed
python3 agent/scripts/clipboard.py write agent/sandbox/myscript.xml --class XMSC
```

Auto-detection reads the first XML element inside the fmxmlsnippet wrapper and maps it to the correct class (e.g. `<Step>` → `XMSS`, `<CustomFunction>` → `XMFN`).

---

## How it works (low-level)

Understanding the encoding helps when diagnosing issues or working outside of `clipboard.py`.

### Reading (FM → XML)

FileMaker stores clipboard data as a record keyed by the full AppleScript class notation: `{«class XMSS»: «data XMSS3C...»}`. The key point is that you must use `«class XMSS»` (not bare `XMSS`) as the property accessor — otherwise AppleScript cannot find the key in the record.

The `clipboard.py` script detects the class, then fetches the value using `osascript -e 'the clipboard as «class XMSS»'`. The `as` coercion form is used rather than `«class XMSS» of (the clipboard)` — the `of` form treats the clipboard as a record and fails when the clipboard's primary type is plain text (which happens when a single text label is copied in Layout Mode). The `as` form locates the requested type regardless of what the primary type is. osascript prints the binary descriptor as:

```
«data XMSS3C666D786D6C736E69707065743C...»
```

The script then:
1. Extracts the hex portion with a regex
2. Converts hex → bytes with `bytes.fromhex()`
3. Decodes as UTF-8
4. Pretty-prints with `xmllint --format -` (included with macOS Xcode command line tools)

The equivalent AppleScript pipeline (as used by the [Typinator](https://www.ergonis.com/typinator) approach and [FmClipTools](https://github.com/DanShockley/FmClipTools)) is:

```applescript
try
    set allowed to {«class XMSS», «class XML2», «class XMLO», «class XMSC», «class XMFD», «class XMFN», «class XMTB», «class XMVL», «class XMTH»}
    set clipboardType to item 1 of item 1 of (clipboard info) as class
    if clipboardType is in allowed then
        -- classString will be e.g. "«class XMSS»" — must use this full form, not bare "XMSS"
        set classString to clipboardType as string
        return do shell script "osascript -e '" & classString & " of (the clipboard)' | sed 's/«data ....//; s/»//' | xxd -r -p | iconv -f UTF-8 -t UTF-8 | xmllint --format -"
    end if
on error errMsg
    return "ERROR: " & errMsg
end try
```

### Writing (XML → FM)

To place an fmxmlsnippet on the clipboard in the correct class for FileMaker to accept:

```applescript
-- Replace XMSS with the appropriate class code and hexdata with the hex-encoded XML
set the clipboard to «data XMSShexdata»
```

From the shell (replace `XMSS` and the file path as needed):

```bash
osascript -e "set the clipboard to «data XMSS$(xxd -p < agent/sandbox/myscript.xml | tr -d '\n')»"
```

`xxd -p` produces the raw hex encoding of the file bytes. The `tr -d '\n'` removes newlines so the hex is a single unbroken string.

---

## Detecting what is on the clipboard

To check whether the clipboard currently holds FileMaker objects (and which type):

```applescript
try
    set allowed to {«class XMSS», «class XML2», «class XMLO», «class XMSC», «class XMFD», «class XMFN», «class XMTB», «class XMVL», «class XMTH»}
    set clipboardType to item 1 of item 1 of (clipboard info) as class
    if clipboardType is in allowed then
        return clipboardType as string  -- returns e.g. "XMSS"
    else
        return "No FileMaker objects on clipboard"
    end if
on error
    return "Clipboard is empty or unreadable"
end try
```

---

## NSPasteboard / PyObjC fast path

`clipboard.py` automatically uses a faster, subprocess-free clipboard path when `pyobjc-framework-Cocoa` is installed. Install it into the project venv:

```bash
pip install pyobjc-framework-Cocoa
```

When available, all clipboard reads and writes go through `NSPasteboard` directly instead of spawning `osascript` subprocesses. This eliminates:
- The `osascript` process launch overhead (significant for large snippets)
- The hex-encode/decode round-trip (`«data XMSS3C...»` → regex → `bytes.fromhex()`)
- Argument-length pressure from very large hex strings passed on the shell command line

Without PyObjC installed the script falls back to the original `osascript` path automatically — no configuration needed.

### How NSPasteboard reads FM binary data

```python
pb = NSPasteboard.generalPasteboard()
pb_type = "CorePasteboardFlavorType 0x584D5353"   # XMSS
data = pb.dataForType_(pb_type)
xml = data.bytes().tobytes().decode('utf-8')
```

### How NSPasteboard reads ut16 menu data

```python
raw_bytes = pb.dataForType_("CorePasteboardFlavorType 0x75743136").bytes().tobytes()
xml = raw_bytes.decode('utf-16')   # BOM is present; Python handles it automatically
```

### How NSPasteboard writes FM data

```python
pb.clearContents()
ns_data = NSData.dataWithBytes_length_(xml_bytes, len(xml_bytes))
pb.setData_forType_(ns_data, "CorePasteboardFlavorType 0x584D5353")
```

Note that `pb.clearContents()` is required before writing — without it, stale data from a previous copy may persist alongside the new data.

---

## Windows clipboard

FileMaker Pro on Windows registers the same four-letter class codes as custom Windows clipboard formats, prefixed with `Mac-` following Apple's Carbon cross-platform clipboard convention (used by FileMaker since FM7).

| macOS class | Windows format name |
|-------------|---------------------|
| `XMSS`      | `Mac-XMSS`          |
| `XMSC`      | `Mac-XMSC`          |
| `XML2`      | `Mac-XML2`          |
| `XMFD`      | `Mac-XMFD`          |
| `XMFN`      | `Mac-XMFN`          |
| `XMTB`      | `Mac-XMTB`          |
| `XMVL`      | `Mac-XMVL`          |
| `XMTH`      | `Mac-XMTH`          |
| `ut16`      | `CF_UNICODETEXT` (plain UTF-16 text: custom menus and menu sets) |

The data payload differs from macOS: FileMaker on Windows prepends a 4-byte little-endian `uint32` length field before the UTF-8 XML. It omits the XML declaration, except in `Mac-XML2` (layout objects):

```
[4 bytes LE uint32 = N]  [N bytes UTF-8 XML starting with <fmxmlsnippet>]
```

On macOS the payload is plain UTF-8 XML with no prefix or length field.

[CLIPBOARD_FORMATS.md](CLIPBOARD_FORMATS.md) is the full Windows spec: the formats FileMaker was seen to write (from FMCuttingBoard's captures), the read and write rules, how menus travel as plain text, the object types not yet captured (`Mac-XMTH` and `Mac-XMLO` above are unconfirmed), and how Proofkit's clipboard tools compare.

### clipboard_win.py

`agent/scripts/clipboard_win.py` provides the same `read` / `write` interface as `clipboard.py` on native Windows, plus `detect`. It calls the Win32 clipboard (`user32.dll` / `kernel32.dll`) through `ctypes`, standard library only, and retries while another process holds the clipboard. It writes the payloads [CLIPBOARD_FORMATS.md](CLIPBOARD_FORMATS.md) specifies and reads the first FileMaker format on the clipboard; custom menus and menu sets travel as `CF_UNICODETEXT` and are recognised by their `FMObjectTransfer` content. It does not run under WSL.

```bash
# Write XML to Windows clipboard (class auto-detected)
python agent/scripts/clipboard_win.py write agent/sandbox/myscript.xml

# Override the class explicitly if needed
python agent/scripts/clipboard_win.py write agent/sandbox/myscript.xml --class XMSC

# Read FM objects from Windows clipboard
python agent/scripts/clipboard_win.py read agent/sandbox/output.xml

# List all custom clipboard formats with their sizes -- run after copying from FM
python agent/scripts/clipboard_win.py detect
```

If FileMaker does not accept a paste, copy the same kind of object in FileMaker Pro and run `detect` to compare the format names and sizes. `read` pretty-prints with `xmllint` when it is installed and returns the XML unchanged otherwise.

Skills and the companion server's `/clipboard` endpoints call `clipboard.py`, which uses `clipboard_win.py` on Windows; call `clipboard_win.py` directly only for `detect`.

[CLIPBOARD_ROUNDTRIP.md](CLIPBOARD_ROUNDTRIP.md) is the manual test that each object type survives copy, read, write and paste with real FileMaker, on Windows and on the Mac, with its results.

---

## Reference

The approach above is derived from [FmClipTools](https://github.com/DanShockley/FmClipTools) by Daniel A. Shockley and Erik Shagdar, which provides a complete AppleScript library for FileMaker clipboard operations including batch conversion, prettifying, and class detection.
