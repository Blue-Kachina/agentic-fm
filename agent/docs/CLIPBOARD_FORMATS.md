# FileMaker clipboard formats on Windows

This page is the spec for reading and writing FileMaker objects on the native Windows clipboard. The native Windows clipboard module is built against it. For how the clipboard works on macOS, and for `clipboard.py` and `clipboard_win.py`, see [CLIPBOARD.md](CLIPBOARD.md).

## Source

The formats come from [FMCuttingBoard](https://github.com/Blue-Kachina/fmcuttingboard) (commit `eb4f8e4`), which captured them by copying each object type in FileMaker Pro on Windows and dumping the clipboard:

- `docs/reference/resources/captured_clipboard_formats.md`: the raw captures (format id, name, size, first bytes).
- `docs/FileMaker-Native-Clipboard-Analysis.md`: the analysis of those captures.
- `shared/data/clipboard-formats.json`: the encoding rules its JetBrains and VS Code plugins write with.

FMCuttingBoard has been tested on Windows only. Its macOS notes (`docs/MacPasteboardResearch.md`, and `"macos": {"status": "unknown"}` in the JSON) are experimental and are not used here. On the Mac, the source is agentic-fm's own `clipboard.py`, which works: the payload is plain UTF-8 XML with no length prefix.

The captures do not record the FileMaker Pro version.

## Formats

FileMaker registers each format by name with `RegisterClipboardFormat`, so the numeric id differs between sessions and machines. Always look a format up by name. The ids below are the ones in the captures.

| Windows format | Object type | fmxmlsnippet content | Captured id | Captured size | First bytes (length prefix) |
| --- | --- | --- | --- | --- | --- |
| `Mac-XMSS` | Script steps | `<fmxmlsnippet type="FMObjectList"><Step …>` | 49929 | 131 | `7F 00 00 00` = 127 |
| `Mac-XMSC` | Scripts | `<fmxmlsnippet type="FMObjectList"><Script …>` | 49932 | 440 | `B4 01 00 00` = 436 |
| `Mac-XMFD` | Fields | `<fmxmlsnippet type="FMObjectList"><Field …>` | 49936 | 2412 | `68 09 00 00` = 2408 |
| `Mac-XMTB` | Tables | `<fmxmlsnippet type="FMObjectList"><BaseTable …>` | 49940 | 4082 | `EE 0F 00 00` = 4078 |
| `Mac-XMFN` | Custom functions | `<fmxmlsnippet type="FMObjectList"><CustomFunction …>` | 49938 | 217 | `D5 00 00 00` = 213 |
| `Mac-XMVL` | Value lists | `<fmxmlsnippet type="FMObjectList"><ValueList …>` | 49984 | 187 | `B7 00 00 00` = 183 |
| `Mac-XML2` | Layout objects | `<?xml …?>` then `<fmxmlsnippet type="LayoutObjectList"><Layout …>` | 49989 | 74224 | `EC 21 01 00` = 74220 |

In every capture the prefix equals the format's size minus 4.

## Payload rule

Every format above holds:

```
[4 bytes: uint32, little-endian = N] [N bytes: UTF-8 XML]
```

- No byte order mark.
- No trailing NUL after the XML.
- Line endings are LF. The large `Mac-XML2` capture has 423 LFs and no CR; the smaller captures have no line breaks at all.
- The XML declaration is present in `Mac-XML2` only. The other six start directly with `<fmxmlsnippet`.

When reading:

- Read the length from the first 4 bytes and decode exactly N bytes as UTF-8. Do not decode up to the end of the block: `GlobalSize` can report a block larger than what was written.
- Accept the payload with or without an XML declaration.
- If the format's size is less than 4 + N, the data is truncated: report an error rather than decode a partial snippet.

When writing:

- Encode the XML as UTF-8 without a BOM, convert CRLF and lone CR to LF, then prefix the byte length (not the character count: `≠` and `¶` are 3 and 2 bytes in UTF-8). Converting CR is safe for the XML itself: an XML parser turns CRLF into LF on reading anyway, so a CR that must survive (such as a `¶` in a calculation) has to be written as `&#13;` already.
- Write the declaration the way FileMaker does: none for the six `FMObjectList` formats, kept for `Mac-XML2`. Whether FileMaker accepts the other variants is not confirmed.
- Put all formats for one copy on the clipboard in one `OpenClipboard` … `CloseClipboard` session, after `EmptyClipboard`.

## Other formats on the clipboard

- A layout-object copy also carries `CF_DIB` (id 8) and `CF_DIBV5` (id 17), a bitmap preview of the selection. Readers ignore them; writers need not produce them.
- FMCuttingBoard also writes the XML as `CF_UNICODETEXT` (UTF-16LE, two-byte NUL terminator) beside the custom format, as a plain-text fallback, and adds the alias `Mac-XML` beside `Mac-XML2`. The captures do not show FileMaker itself putting either on the clipboard for these object types, and agentic-fm's `clipboard_win.py` writes the custom format alone.

## Not yet captured

These object types have no FileMaker capture on Windows. Their format names and encodings are unconfirmed, and the tools disagree:

| Object type | `clipboard_win.py` writes | Proofkit writes | Notes |
| --- | --- | --- | --- |
| Themes | `Mac-XMTH`, length-prefixed UTF-8 | `Mac-XMLT`, length-prefixed UTF-8 | The names differ; at most one can be right. |
| Custom menus | `Mac-ut16`, UTF-16 with BOM, no prefix | `Mac-XMGR`, length-prefixed UTF-8 | On the Mac, menus are UTF-16 text (`«class ut16»`), see [CUSTOM_MENUS.md](CUSTOM_MENUS.md). |
| Custom menu sets | `Mac-ut16`, as custom menus | not supported | |
| Legacy layout objects (`XMLO`) | `Mac-XMLO`, length-prefixed UTF-8 | not supported | FMCuttingBoard writes `Mac-XML` as its legacy alias instead. |

To capture one: copy the object in FileMaker Pro on Windows, then list the clipboard's formats and save the raw bytes of each `Mac-` format. FMCuttingBoard's "Diagnostics: Dump Clipboard Formats" and "Save Raw Clipboard Capture" commands do both (VS Code or JetBrains); `clipboard_win.py detect` lists the names from WSL2.

Also not covered by the captures: non-ASCII text (all captured payloads were ASCII, so UTF-8 is inferred, not observed), and differences between FileMaker Pro versions.

## Proofkit's clipboard tools

Proofkit's MCP server has `clipboard_write`, `clipboard_read` and `clipboard_list_formats`. They were checked against this spec on 2026-11-02, on Windows 11, without FileMaker: a write was followed by a raw dump of the Win32 clipboard, and a read was given payloads shaped like the FMCuttingBoard captures. Nothing was pasted into FileMaker.

| Object type | `clipboard_write` (with `elementType`) | `clipboard_read` |
| --- | --- | --- |
| Script steps | `Mac-XMSS`, prefix correct, no BOM, no declaration | Correct, including `≠` and `¶` |
| Scripts | `Mac-XMSC`, prefix correct | not tested |
| Fields | `Mac-XMFD`, prefix correct | not tested |
| Tables | `Mac-XMTB`, prefix correct | not tested |
| Custom functions | `Mac-XMFN`, prefix correct | not tested |
| Value lists | `Mac-XMVL`, prefix correct | not tested |
| Layout objects | `Mac-XML2`, prefix correct, declaration kept | Correct, declaration and LFs kept |
| Themes | `Mac-XMLT` (see above) | not tested |
| Custom menus | `Mac-XMGR` (see above) | `Mac-ut16` is not recognised: `clipboard_list_formats` returns nothing |

For the seven captured formats, Proofkit's payloads match this spec byte for byte. It writes the custom format alone, with no `CF_UNICODETEXT`.

Where it falls short:

- Detecting the type from the XML only works for script steps. A whole `<Script>` is detected as script steps and written as `Mac-XMSS`; tables, fields, custom functions and value lists fail with "Unable to infer FileMaker element type". Always pass `elementType`.
- It keeps an XML declaration it is given, for every type.
- It is an MCP tool: the agent can call it, but Python scripts, the companion's `/clipboard` endpoints and `deploy.py` cannot.

### What Proofkit could replace

- **Native Windows clipboard module:** not replaced. On Windows, an agent with Proofkit connected can use it as a stop-gap for the seven captured formats, passing `elementType`. The module is still needed for a CLI that matches `clipboard.py`, for the companion and for `deploy.py`. Decided 2026-10-23: our own Python module stays the v1 path.
- **One clipboard entry point:** not replaced. It routes `clipboard.py` and the companion, which cannot call an MCP tool.
- **Clipboard round-trip test:** not replaced, but useful: Proofkit is an independent writer, so a paste that fails with our module and works with Proofkit (or the reverse) points at the encoding.
- **Running a FileMaker script, deploy tiers, the "Agentic-fm Paste" Windows branch:** out of its scope; it has no clipboard role there.
