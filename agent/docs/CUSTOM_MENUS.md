# Custom Menus

This document covers the custom menu creation and modification workflow, covering clipboard format, XML structure, critical fields, and how this fits into the broader agent process.

---

## Clipboard format

FileMaker menu objects use a completely different clipboard format from all other FM objects. Scripts, layouts, fields, etc. are stored as binary FM descriptor classes (`XMSS`, `XML2`, etc.). **Menu objects are stored as UTF-16 Unicode text** (`«class ut16»`).

This means `clipboard.py` handles menus via a separate code path:

- **Write**: strips the XML declaration, re-encodes the content as UTF-16 with BOM, pushes as `«data ut16XXXX»`
- **Read**: `osascript` returns the raw XML text for `ut16` (not a binary descriptor), so stdout is decoded directly as UTF-8

On Windows, FileMaker also copies menus as plain Unicode text (`CF_UNICODETEXT`, UTF-16LE without a BOM), and `clipboard.py` (through `clipboard_win.py`) reads and writes them there, with the same commands. See [CLIPBOARD_FORMATS.md](CLIPBOARD_FORMATS.md#custom-menus-and-menu-sets).

Auto-detection in `clipboard.py` works by checking for `<CustomMenu` or `<CustomMenuSet` elements in the XML — these are checked before `<Step>` to avoid false-positive `XMSS` detection (menu XML contains `<Step>` elements inside action blocks).

The source file can be UTF-8 or UTF-16; `clipboard.py` detects the BOM and handles both.

---

## XML structure

Menu objects are wrapped in `<FMObjectTransfer>` (not `<fmxmlsnippet>`). There are two distinct object types:

### CustomMenuSet
The container that groups menus and is assigned per-layout. Stored in `xml_parsed/custom_menu_sets/`.

```xml
<FMObjectTransfer ...>
    <CustomMenuSetCatalog membercount="1">
        <UUID ...>CATALOG-UUID</UUID>
        <CustomMenuSet name="agentic-fm" id="2" comment="...">
            <UUID ...>MENU-SET-UUID</UUID>
            <CustomMenuList membercount="5">
                <CustomMenuReference name="Edit" id="27"></CustomMenuReference>
                <CustomMenuReference name="Format" id="31"></CustomMenuReference>
                <!-- etc. -->
            </CustomMenuList>
        </CustomMenuSet>
    </CustomMenuSetCatalog>
    <PasteIndexList membercount="1">
        <Object id="2"></Object>
    </PasteIndexList>
</FMObjectTransfer>
```

### CustomMenu
An individual menu with its items. Stored in `xml_parsed/custom_menus/`.

```xml
<FMObjectTransfer ...>
    <CustomMenuCatalog membercount="1">
        <UUID modifications="N" ...>CATALOG-UUID</UUID>
        <CustomMenu name="Format" id="31">
            <UUID modifications="N" ...>MENU-UUID</UUID>
            <MenuItemList membercount="N">
                <!-- menu items -->
            </MenuItemList>
            <Conditions>
                <Install><Calculation><Text><![CDATA[1]]></Text></Calculation></Install>
            </Conditions>
            <Comment></Comment>
            <Options browseMode="True" findMode="True" previewMode="True">
                <Override>
                    <Title><Calculation><Text><![CDATA["Format"]]></Text></Calculation></Title>
                </Override>
            </Options>
        </CustomMenu>
    </CustomMenuCatalog>
    <PasteIndexList membercount="1">
        <Object id="31"></Object>
    </PasteIndexList>
</FMObjectTransfer>
```

### Menu item — custom action
```xml
<CustomMenuItem hash="..." index="0" isSubMenuItem="False" isSeparatorItem="False">
    <UUID>ITEM-UUID</UUID>
    <action>
        <Step enable="True" id="1" name="Perform Script">
            <StepText>Perform Script [ "Script Name"; Parameter: &quot;param&quot; ]</StepText>
            <CurrentScript value="Pause"></CurrentScript>
            <Calculation><![CDATA["param"]]></Calculation>
            <DisplayCalculation>
                <Chunk type="NoRef">&quot;param&quot;</Chunk>
            </DisplayCalculation>
            <Script id="NNN" name="Script Name"></Script>
        </Step>
    </action>
    <Conditions>
        <Install><Calculation><Text><![CDATA[1]]></Text></Calculation></Install>
    </Conditions>
    <Name>
        <Calculation><Text><![CDATA["Menu Item Label"]]></Text></Calculation>
    </Name>
    <Shortcut key="75" modifier="5"></Shortcut>
    <Command id="0"></Command>
    <Override name="True" action="True" Shortcut="True"></Override>
</CustomMenuItem>
```

### Menu item — standard FileMaker command override
```xml
<CustomMenuItem hash="..." index="0" isSubMenuItem="False" isSeparatorItem="False">
    <UUID>ITEM-UUID</UUID>
    <action>
        <!-- Perform Script step as above -->
    </action>
    <Conditions>
        <Install><Calculation><Text><![CDATA[1]]></Text></Calculation></Install>
    </Conditions>
    <Shortcut key="90" modifier="4"></Shortcut>
    <Command name="Undo" id="49320"></Command>
    <Override name="False" action="True" Shortcut="False"></Override>
</CustomMenuItem>
```

### Separator
```xml
<CustomMenuItem hash="..." index="N" isSubMenuItem="False" isSeparatorItem="True">
    <UUID>ITEM-UUID</UUID>
    <Conditions>
        <Install><Calculation><Text><![CDATA[1]]></Text></Calculation></Install>
    </Conditions>
    <Override name="False" action="False" Shortcut="False"></Override>
</CustomMenuItem>
```

---

## Critical fields

### UUIDs — must match the solution

FileMaker uses UUIDs to identify objects. When pasting a `CustomMenu`, FileMaker matches it to the existing menu in the solution via the `CustomMenu > UUID`. If the UUID is wrong or made up, the paste silently does nothing.

**Always obtain real UUIDs from the existing XML** in `xml_parsed/custom_menus/` or by reading from the clipboard after copying in FileMaker.

| Field | Source |
|---|---|
| `FMObjectTransfer UUID` | Solution file UUID — constant across all files |
| `CustomMenuCatalog > UUID` | The solution's menu catalog UUID — copy from any existing `CustomMenu` export |
| `CustomMenu > UUID` | The specific menu's UUID — must come from that menu's FM export |
| `CustomMenuItem > UUID` | Can be generated; FM reassigns on paste |
| `CustomMenuItem hash` | Can be placeholder; FM may recalculate |
| `<SourceUUID>` | FM lineage tracking — added by FM when a menu is duplicated. Not required in generated XML; omit it. |

### Shortcut modifier values

Modifier bits are additive. One value is stored in the file, and each platform reads the bits as its own keys:

| Bit | macOS | Windows |
|---|---|---|
| `1` | Shift | Shift |
| `2` | Ctrl | Alt |
| `4` | Cmd | Ctrl |
| `8` | Opt | not observed |

| Modifier value | macOS keys | Windows keys |
|---|---|---|
| `4` | Cmd | Ctrl |
| `5` | Cmd+Shift | Ctrl+Shift |
| `6` | Cmd+Ctrl | Ctrl+Alt |
| `7` | Cmd+Ctrl+Shift | Ctrl+Alt+Shift |
| `12` | Cmd+Opt | not observed |
| `13` | Cmd+Opt+Shift | not observed |

The Windows column comes from a menu made and copied in FileMaker Pro 26.0.3 on Windows 11 (2026-11-02): Ctrl+K was stored as `key="75" modifier="4"`, Ctrl+Shift+K as `5`, Ctrl+Alt+K as `6`, Ctrl+/ as `47`/`4` and Ctrl+Up Arrow as `57349`/`4`. Value `7` follows from the bits; `8` (Opt) did not come up on Windows. Whether the Windows Specify Shortcut dialog can set bit 8 at all is not confirmed.

**Constraints:**
- **Option-alone modifier (8) is not supported** in FileMaker custom menus. FM ignores it silently.
- **Alt alone is refused on Windows**: the Specify Shortcut dialog in FileMaker Pro 26.0.3 does not accept Alt+K.
- **Ctrl+Alt on Windows** (modifier `6`, Cmd+Ctrl on the Mac) is AltGr on many non-US keyboard layouts, where it types characters instead of firing the shortcut.
- **Cmd+Opt combinations work**, but Cmd+Ctrl (modifier=6) is often used as a substitute for Monaco shortcuts that use Opt (e.g. Move Line Up/Down) to avoid conflicts.
- **Some Cmd shortcuts conflict with built-in FM shortcuts** (e.g. Cmd+/ is reserved by FM). Use Cmd+Ctrl as a substitute modifier in these cases.

### Key code values

Key codes in FileMaker custom menus are **ASCII character codes**, not Windows Virtual Key codes. They are the same on both platforms: FileMaker on Windows stored `/` as 47 and Up Arrow as 57349.

| Key | Code | Notes |
|---|---|---|
| A–Z | 65–90 | Standard ASCII uppercase |
| `/` | 47 | ASCII — NOT Windows VK 191 |
| `[` | 91 | ASCII — NOT Windows VK 219 |
| `]` | 93 | ASCII — NOT Windows VK 221 |
| Arrow Up | 57349 | FM-specific large value |
| Arrow Down | 57351 | FM-specific large value |

**Common mistake**: Using Windows VK codes (191, 219, 221) for punctuation keys. These do not work in FileMaker. Always use ASCII codes.

### Format menu shortcut reference (agentic-fm)

Confirmed working shortcuts from FileMaker export (`Format-fixed.xml`), on the Mac. The Windows column is what the same stored values mean there, by the bit table above; it has not been tried in the agentic-fm web viewer on Windows.

| Menu item | Monaco action ID | Key | Modifier | macOS keys | Windows keys |
|---|---|---|---|---|---|
| Toggle Line Comment | `editor.action.commentLine` | 47 (`/`) | 6 | Cmd+Ctrl+/ | Ctrl+Alt+/ |
| Toggle Block Comment | `editor.action.blockComment` | 65 (`A`) | 5 | Cmd+Shift+A | Ctrl+Shift+A |
| Indent Lines | `editor.action.indentLines` | 93 (`]`) | 4 | Cmd+] | Ctrl+] |
| Outdent Lines | `editor.action.outdentLines` | 91 (`[`) | 4 | Cmd+[ | Ctrl+[ |
| Move Line Up | `editor.action.moveLinesUpAction` | 57349 (↑) | 6 | Cmd+Ctrl+↑ | Ctrl+Alt+↑ |
| Move Line Down | `editor.action.moveLinesDownAction` | 57351 (↓) | 6 | Cmd+Ctrl+↓ | Ctrl+Alt+↓ |
| Copy Line Up | `editor.action.copyLinesUpAction` | 57349 (↑) | 7 | Cmd+Ctrl+Shift+↑ | Ctrl+Alt+Shift+↑ |
| Copy Line Down | `editor.action.copyLinesDownAction` | 57351 (↓) | 7 | Cmd+Ctrl+Shift+↓ | Ctrl+Alt+Shift+↓ |
| Delete Line | `editor.action.deleteLines` | 75 (`K`) | 5 | Cmd+Shift+K | Ctrl+Shift+K |

### `<Override>` attribute rules

| Scenario | `name` | `action` | `Shortcut` |
|---|---|---|---|
| Custom label + custom action + shortcut | `True` | `True` | `True` |
| Custom label + custom action, no shortcut | `True` | `True` | `False` |
| Standard FM command overridden | `False` | `True` | `False` |
| Separator | `False` | `False` | `False` |

### `<Base>` element

Present only when the custom menu is explicitly based on a standard FM menu (e.g. Edit, Format). Absence is valid and common. Do not add it unless the original FM export includes it.

```xml
<Base name="Edit" value="3"></Base>
```

Standard FM menu values: File=2, Edit=3, View=4, Insert=5, Format=6, Records=7, Scripts=8, Tools=9, Window=10, Help=11.

---

## Workflow

### Modifying an existing menu

1. Locate the menu in `xml_parsed/custom_menus/` by name or ID
2. Read it to get the real UUIDs
3. Copy to `agent/sandbox/` and modify
4. Write to clipboard: `python agent/scripts/clipboard.py write agent/sandbox/<menu>.xml`
5. In FileMaker, open Manage > Custom Menus, select the target menu, paste

### Creating a new menu from scratch

1. In FileMaker, create an empty custom menu (gives it a real UUID in the solution)
2. Copy it from FileMaker, save via: `python agent/scripts/clipboard.py read agent/sandbox/<menu>-original.xml`
3. Use the real `CustomMenu > UUID` and `CustomMenuCatalog > UUID` from that file as the basis for the generated XML
4. Build the full menu XML following the patterns above
5. Write and paste as above

### Reading a menu from the clipboard

```bash
python agent/scripts/clipboard.py read agent/sandbox/output.xml
```

Note: `osascript` returns `ut16` clipboard content as plain UTF-8 text (not a binary descriptor), so the read path decodes stdout directly without hex parsing.

---

## The agentic-fm menu passthrough pattern

The menu system in this project uses a single bridge script to route all menu actions to the Monaco editor in the web viewer:

- **Script 271 "Agentic-fm Menu"** — receives the action ID as `Get(ScriptParameter)` and calls `Perform JavaScript in Web Viewer` targeting the `agentic-fm` web viewer object, invoking `triggerEditorAction(actionId)`
- **`window.triggerEditorAction`** — exposed globally in `EditorPanel.tsx`, calls `editor.trigger('fm', actionId, null)` on the Monaco instance
- Each menu item calls script 271 with a Monaco action ID as its parameter (e.g. `"actions.find"`, `"editor.action.commentLine"`)

See `webviewer/src/editor/EditorPanel.tsx` for the global exposure and `filemaker/custom_menu/custom_menus.xml` for a complete working example.

