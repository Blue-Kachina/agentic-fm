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

[CLIPBOARD_FORMATS.md](CLIPBOARD_FORMATS.md#not-yet-captured) lists types whose Windows format is unconfirmed. They are not part of the pass/fail, but while FileMaker is open, try each one FileMaker lets you copy (custom menus and menu sets in Manage Custom Menus, themes if FileMaker offers Copy for them):

1. Copy it in FileMaker and run `clipboard_win.py detect`. Record every `Mac-` format name and size.
2. Run steps 3 to 7 above and record what happened.

These results settle which names are right (`Mac-XMTH` or `Mac-XMLT` for themes; `Mac-ut16` or `Mac-XMGR` for menus) and go back into CLIPBOARD_FORMATS.md.

## When something fails

Write down the step that failed, the command's full output, and what FileMaker did (error text, or pasted as plain text). Keep the XML file. Each failure gets a follow-up unit in the roadmap; note its ID in the "Follow-up" column.

## Results

### Windows

FileMaker Pro version: · Windows version: · Run by: · Date:

| Object type | File | FM format (size) | Read | Write format (size) | Paste | Follow-up |
| --- | --- | --- | --- | --- | --- | --- |
| Script steps | `steps.xml` | | | | | |
| Scripts | `script.xml` | | | | | |
| Fields | `field.xml` | | | | | |
| Tables | `table.xml` | | | | | |
| Custom functions | `function.xml` | | | | | |
| Value lists | `valuelist.xml` | | | | | |
| Layout objects | `layout.xml` | | | | | |
| *Extra:* custom menu | `menu.xml` | | | | | |
| *Extra:* menu set | `menuset.xml` | | | | | |
| *Extra:* theme | `theme.xml` | | | | | |

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
