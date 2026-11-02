# Clipboard round-trip test

A manual test that the clipboard path works with real FileMaker: a person copies each object type in FileMaker Pro, reads it with `clipboard.py`, writes it back, and pastes it. It is run on Windows, then on a Mac, and the results are recorded below. The automated tests (`test_clipboard_win.py`, `test_clipboard.py`, `test_companion_clipboard.py`) cover the encoding, but not whether FileMaker accepts what we write.

The formats being tested are in [CLIPBOARD_FORMATS.md](CLIPBOARD_FORMATS.md).

## Before you start

- A scratch FileMaker file you can change freely (a new, empty file is fine), open in FileMaker Pro.
- A terminal at the agentic-fm root. Commands below use `python`; on the Mac, use `python3`.
- Make the folder for the files: `agent/sandbox/roundtrip/` (git-ignored).
- In the scratch file, create one object of each type below, and put the marker text **`a ≠ b ¶ café`** in each one: it checks that multi-byte characters survive (`≠` is 3 bytes in UTF-8, `¶` and `é` are 2).

| Object type | What to create | Where the marker goes |
| --- | --- | --- |
| Script steps | A script with a `# (comment)` step and a `Set Variable [ $x ; "a ≠ b ¶ café" ]` step | Comment text and calculation |
| Scripts | That same script | Same |
| Fields | A text field `RoundTrip` in a table `RoundTrip` | Field comment |
| Tables | The table `RoundTrip` | Its field's comment |
| Custom functions | `RoundTrip ( p )` returning `p & " a ≠ b ¶ café"` | Calculation |
| Value lists | `RoundTrip`, custom values `a ≠ b` and `café` on two lines | Values |
| Layout objects | A text object reading `a ≠ b ¶ café` and the `RoundTrip` field, on any layout | Text object |

Record the FileMaker Pro version and the OS version in the results table.

## Procedure (each object type)

Use the file name from the "File" column of the results table, e.g. `steps.xml`.

1. **Copy from FileMaker.** Select the object (in the paste context listed below) and copy it (Ctrl+C / ⌘C).
2. **Note what FileMaker wrote** (Windows only). Run `python agent/scripts/clipboard_win.py detect` and note the `Mac-` format name and its size.
3. **Read.** Run `python agent/scripts/clipboard.py read agent/sandbox/roundtrip/<file>`.
   Pass when the file opens with `<fmxmlsnippet`, holds the expected element (see the table), and the marker reads `a ≠ b ¶ café` exactly (not `a â‰  b`, `a ? b` or similar).
4. **Clear the clipboard.** Copy a word of plain text from any other app, so that the paste below cannot reuse FileMaker's own data.
5. **Write.** Run `python agent/scripts/clipboard.py write agent/sandbox/roundtrip/<file>`.
   Pass when it prints `Clipboard ready` with the expected format (Windows: the same `Mac-` name as step 2).
6. **Compare** (Windows only). Run `clipboard_win.py detect` again. Note the size: the same size as step 2 means the bytes match what FileMaker wrote. A different size is not a failure by itself (`read` pretty-prints when `xmllint` is installed), but record it.
7. **Paste into FileMaker** in the paste context. Rename or delete the original first where FileMaker would refuse a duplicate name (or paste into a second scratch file).
   Pass when FileMaker creates the object (not plain text, no error), and the marker reads `a ≠ b ¶ café` in it.

A type passes when steps 3, 5 and 7 pass.

| Object type | Paste context | Expected element | Format |
| --- | --- | --- | --- |
| Script steps | Script Workspace, a script's step list | `<Step>` | `XMSS` |
| Scripts | Script Workspace, the scripts list | `<Script>` | `XMSC` |
| Fields | Manage Database, Fields tab | `<Field>` | `XMFD` |
| Tables | Manage Database, Tables tab | `<BaseTable>` | `XMTB` |
| Custom functions | Manage Custom Functions | `<CustomFunction>` | `XMFN` |
| Value lists | Manage Value Lists | `<ValueList>` | `XMVL` |
| Layout objects | A layout in Layout mode | `<Layout>` | `XML2` |

## Extra: object types with no capture

[CLIPBOARD_FORMATS.md](CLIPBOARD_FORMATS.md#not-yet-captured) lists types whose Windows format is unconfirmed. They are not part of the pass/fail, but while FileMaker is open, try each one FileMaker lets you copy (custom menus and menu sets in Manage Custom Menus, where Ctrl+C works although there is no Copy button; themes if FileMaker offers Copy for them):

1. Copy it in FileMaker and run `clipboard_win.py detect`. Record every `Mac-` format name and size. `detect` lists only custom formats: if it finds none, check whether FileMaker put the object on the clipboard as plain text (as it does with menus on Windows).
2. Run steps 3 to 7 above and record what happened.

These results settle which names are right (`Mac-XMTH` or `Mac-XMLT` for themes; `Mac-ut16` or `Mac-XMGR` for menus) and go back into CLIPBOARD_FORMATS.md.

## When something fails

Write down the step that failed, the command's full output, and what FileMaker did (error text, or pasted as plain text). Keep the XML file. Each failure gets a follow-up unit in the roadmap; note its ID in the "Follow-up" column.

## Results

### Windows

FileMaker Pro version: 26.0.3.307 · Windows version: 11 (10.0.26300.9711) · Run by: Matthew Leering · Date: 2026-11-02

`xmllint` was not installed, so `read` saved FileMaker's XML unchanged and an equal size in step 6 means the bytes match.

| Object type | File | FM format (size) | Read | Write format (size) | Paste | Follow-up |
| --- | --- | --- | --- | --- | --- | --- |
| Script steps | `steps.xml` | `Mac-XMSS` (496) | Pass | `Mac-XMSS` (496) | Pass; re-copied, byte-identical to the first copy | |
| Scripts | `script.xml` | `Mac-XMSC` (608) | Pass | `Mac-XMSC` (608) | Pass | |
| Fields | `field.xml` | `Mac-XMFD` (821) | Pass | `Mac-XMFD` (821) | Pass | |
| Tables | `table.xml` | `Mac-XMTB` (1642) | Pass | `Mac-XMTB` (1642) | Pass | |
| Custom functions | `function.xml` | `Mac-XMFN` (220) | Pass | `Mac-XMFN` (220) | Pass | |
| Value lists | `valuelist.xml` | `Mac-XMVL` (191) | Pass; the line break between values is `&#13;` in the XML | `Mac-XMVL` (191) | Pass | |
| Layout objects | `layout.xml` | `Mac-XML2` (14952), with the XML declaration and LFs | Pass | `Mac-XML2` (14952) | Pass | |
| *Extra:* custom menu | `menu.xml` | `CF_UNICODETEXT` (3806), no `Mac-` format | **Fail**: no FileMaker format found | `Mac-ut16` (3728) | **Fail**: nothing pasted. FileMaker's own bytes written back as `CF_UNICODETEXT` paste correctly | P4-08 Menus as Unicode text on Windows (#54142) |
| *Extra:* menu set | `menuset.xml` | `CF_UNICODETEXT` (2380), no `Mac-` format | **Fail**, as menus | not run | FileMaker's own bytes written back as `CF_UNICODETEXT` paste correctly | Same unit |
| Custom menu, rerun after P4-08 | `menu2.xml` | `CF_UNICODETEXT` (3832) | Pass | `CF_UNICODETEXT` (3832) | Pass | |
| Menu set, rerun after P4-08 | `menuset2.xml` | `CF_UNICODETEXT` (2404) | Pass | `CF_UNICODETEXT` (2404) | Pass | |
| *Extra:* theme | — | Nothing: Ctrl+C in Manage Themes copies nothing | — | — | — | None |

Menus and menu sets were copied with Ctrl+C in Manage Custom Menus, which has no Copy button. The first run's menu failures were fixed by P4-08 (#54142); the reruns (same day, same versions) pass.

### Mac

FileMaker Pro version: · macOS version: · PyObjC installed (yes/no): · Run by: · Date:

| Object type | File | Read | Write format | Paste | Follow-up |
| --- | --- | --- | --- | --- | --- |
| Script steps | `steps.xml` | | | | |
| Scripts | `script.xml` | | | | |
| Fields | `field.xml` | | | | |
| Tables | `table.xml` | | | | |
| Custom functions | `function.xml` | | | | |
| Value lists | `valuelist.xml` | | | | |
| Layout objects | `layout.xml` | | | | |
