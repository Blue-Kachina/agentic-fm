# Layout Preview System

How the agent designs, previews, and produces FileMaker layout objects. This document covers the full pipeline: theme extraction, layout XML summarisation, webviewer preview rendering, and the two output paths (native XML2 and Web Viewer HTML).

---

## Architecture Overview

```
                                    ┌─────────────────────────┐
                                    │   FileMaker Solution    │
                                    │                         │
                                    │  fm-saxml docs: model   │──── Theme metadata, layout objects
                                    │  and raw/ XML           │──── Theme CSS, layout XML
                                    └─────────────────────────┘
                                                │
                                    ┌───────────┴───────────┐
                                    │                       │
                               extract_theme.py      layout_to_summary.py
                                    │                       │
                                    ▼                       ▼
                        ┌───────────────────┐   ┌────────────────────┐
                        │  agent/context/   │   │  agent/context/    │
                        │  {solution}/      │   │  {solution}/       │
                        │                   │   │  layouts/          │
                        │  theme.css        │   │  {Layout}.json     │
                        │  theme-web.css    │   └────────────────────┘
                        │  theme-manifest   │        78% smaller
                        │  theme-classes    │        than raw XML
                        └───────────────────┘
                                    │
                          Agent reads theme
                          constraints + layout
                          summaries
                                    │
                                    ▼
                        ┌───────────────────┐
                        │  Agent generates  │
                        │  HTML preview     │
                        │  using theme CSS  │
                        └───────────────────┘
                                    │
                    POST /webviewer/push
                    type: "layout-preview"
                    content: HTML
                    styles: theme-web.css
                                    │
                                    ▼
                        ┌───────────────────┐
                        │    Webviewer      │
                        │  AgentOutputPanel │
                        │                   │
                        │  Shadow DOM with  │
                        │  theme CSS        │
                        │  isolation        │
                        └───────────────────┘
                                    │
                           Developer iterates
                           ("move this", "use
                            that style", etc.)
                                    │
                                    ▼
                    ┌───────────────┴───────────────┐
                    │                               │
             Native FM path                  Web Viewer path
                    │                               │
                    ▼                               ▼
           XML2 fmxmlsnippet              Self-contained HTML
           (absolute positions,           (same theme CSS,
            FM object types,               responsive optional,
            LocalCSS classes)              FM bridge scripts)
                    │                               │
                    ▼                               ▼
           Paste into Layout Mode          Set Web Viewer URL
```

---

## 1. Theme Extraction

**Tool**: `agent/scripts/extract_theme.py`

FileMaker themes define the visual language of a solution — fonts, colors, borders, padding, icon styles. The extraction tool reads the solution's fm-saxml docs (theme metadata from the model, the CSS from the theme's raw XML, style classes from the layouts' raw XML) and produces web-usable output files in `agent/context/{solution}/`.

### Usage

```bash
python3 agent/scripts/extract_theme.py -s "Invoice Solution" --docs-root <folder>    # the default theme
python3 agent/scripts/extract_theme.py -s "Invoice Solution" --theme "Apex Blue"     # another theme
python3 agent/scripts/extract_theme.py -s "Invoice Solution" --list                  # list the themes
```

### Output Files

| File | Purpose | Used by |
|---|---|---|
| `theme.css` | Faithful extraction of the FM theme CSS with shorthand consolidation and `-fm-` property annotations | Reference — what FM actually uses |
| `theme-web.css` | Web-compatible translation — all `-fm-` properties mapped to web equivalents | Webviewer Shadow DOM for preview rendering |
| `theme-manifest.json` | Structured metadata: theme identity, color palette, layout builder constants, 92 named styles | Agent reads for design constraints |
| `theme-classes.json` | 42 named style classes found across all layout files, with layout usage tracking | Agent reads to know available styles |

### CSS Processing Pipeline

The raw theme CSS goes through three transformations:

1. **FM-specific annotation** — every line containing `-fm-` properties gets a `/* FM-specific */` comment in `theme.css`
2. **Shorthand consolidation** — directional properties (border-top/right/bottom/left-color, etc.) are collapsed into shorthand (`border-color`, `margin`, `padding`, `border-radius`, `border-style`, `border-width`). Reduces CSS by ~32%.
3. **Web translation** (for `theme-web.css` only) — FM-specific properties are mapped to web equivalents:

| FM Property | Web Equivalent |
|---|---|
| `-fm-font-family(Name-Variant,...)` | `font-family` + `font-weight` (variant parsed: Bold→bold, Light→300, Medium→500) |
| `-fm-icon: radio` | CSS `mask-image` with inline SVG data URI (filled circle) |
| `-fm-icon: modern-check` | CSS `mask-image` with inline SVG data URI (checkmark) |
| `-fm-icon: up-arrow` / `down-arrow` | CSS `mask-image` with inline SVG data URI (chevrons) |
| `-fm-icon: calendar` | CSS `mask-image` with inline SVG data URI (calendar grid) |
| `-fm-icon-color: rgba(...)` | `color: rgba(...)` (drives `currentColor` in the SVG mask) |
| `-fm-icon-padding: value` | `padding: value` |
| `-fm-text-vertical-align: center` | `display: flex; align-items: center` |
| `-fm-tab-spacing: value` | `gap: value` |
| `-fm-portal-alt-background: true` | Comment: apply `:nth-child(even)` background |
| `-fm-use-portal-current-row-style` | Comment: apply `:hover` styles |

The icon SVGs use `currentColor` for fill, so they inherit the `-fm-icon-color` value via the translated `color` property.

### Theme as Design Constraint

The theme CSS is not just for rendering — it constrains what the agent can suggest. The agent must only recommend style classes that exist in `theme-classes.json`. If no class fits a design need, the agent suggests the closest match and notes the limitation. Creating new theme classes requires manual work in FileMaker's Theme editor.

### Layout Builder Constants

The `theme-manifest.json` includes layout builder spacing values from the theme's metadata:

```json
"layoutBuilder": {
    "kBaseFontSize": 16,
    "kPartPaddingLeft": 20,
    "kPartPaddingRight": 20,
    "kVerticalFieldSpacing": 4,
    "kBodyPartPaddingTop": 11,
    "kFirstPartPaddingTop": 20,
    ...
}
```

These are the exact spacing constants FM uses when auto-generating layouts. The agent uses them for consistent positioning when generating XML2 output.

---

## 2. Layout XML Summarisation

**Tool**: `agent/scripts/layout_to_summary.py`

FileMaker layout XML is extremely verbose — a 52-object layout produces 2,310 lines of XML. Most of that is binary icon data, hash attributes, numeric option bitfields, and deeply nested formatting blocks. The summarisation tool extracts only design-relevant data into compact JSON.

### Usage

```bash
python3 agent/scripts/layout_to_summary.py -s "Invoice Solution" --docs-root <folder>                     # all layouts
python3 agent/scripts/layout_to_summary.py -s "Invoice Solution" --layout "Invoices Details" --print      # one, to stdout
python3 agent/scripts/layout_to_summary.py <layout.xml>                                                   # any layout XML file
```

It reads the solution's fm-saxml docs (through `solution_context.py`; `-s` also takes the docs folder, and `--file` picks a file of a multi-file build): object positions, field bindings, display styles, value lists, portals and button scripts from the model, the rest (styles, text, icons, conditions, triggers) from the layout's raw XML. `--layout` takes a name or docId, or part of a name.

### What It Extracts

For each layout object:
- **Type**: Edit Box, Text, Button, Button Bar, Portal, Rectangle, Container, Drop-down, Radio Button Set, etc.
- **Bounds**: `[top, left, bottom, right]` — absolute pixel coordinates
- **Field binding**: `"Invoices::Number"` with field ID
- **Display style**: editBox, dropDown, popUp, checkbox, radioButtons, calendar, concealedEditBox
- **Style class**: the LocalCSS class name (e.g., `medium_strong_field`)
- **Button wiring**: script name, script ID, parameter, tooltip
- **Portal config**: related TO, visible row count, nested objects
- **Conditions**: hide-when calculations, conditional format count
- **Value list**: name reference for dropdowns/radio sets

### What It Strips

- Binary icon data (base64 SVG, hex glyphs) — replaced with `"hasIcon": true`
- Hash attributes
- Numeric option bitfields
- Extended formatting blocks (number/date/time display options)
- Tab order metadata
- Accessibility label references

### Token Savings

Tested against the Invoice Solution (26 layouts):

```
XML total:  856 KB
JSON total: 185 KB (78% reduction)
```

The Invoices Details layout specifically: 116 KB → 23 KB (80% reduction, 52 objects).

### Output Format

```json
{
  "layout": "Invoices Details",
  "id": 34,
  "width": 1112,
  "table": "Invoices",
  "theme": "com.filemaker.theme.apex_blue",
  "parts": [
    {
      "type": "Top Navigation",
      "height": 55,
      "style": "colored_part",
      "objects": [
        {
          "type": "Container",
          "bounds": [0, 0, 55, 232],
          "style": "large_text",
          "field": "Admin::Logo Global",
          "fieldId": 25
        },
        {
          "type": "Button Bar",
          "bounds": [0, 274, 55, 838],
          "style": "underlined_inverse_bar",
          "activeSegment": 338,
          "buttons": [
            {
              "type": "Button",
              "label": "Dashboard",
              "script": "Navigation",
              "scriptId": 6,
              "param": "dashboard"
            }
          ]
        }
      ]
    }
  ]
}
```

---

## 3. Webviewer Preview Rendering

The webviewer's AgentOutputPanel supports a `layout-preview` payload type that renders an HTML layout mock inside a Shadow DOM with the FM theme CSS injected for style isolation.

### Push Mechanism

```bash
curl -s -X POST -H "Content-Type: application/json" \
  -d '{
    "type": "layout-preview",
    "content": "<div class=\"fm-layout-preview\">...</div>",
    "styles": "/* theme-web.css content */",
    "repo_path": "/path/to/repo"
  }' \
  http://localhost:8765/webviewer/push
```

- `content` — the HTML layout mock, using FM theme class names as CSS classes
- `styles` — the `theme-web.css` content, injected into the Shadow DOM `<style>` tag
- The Shadow DOM isolates the FM theme CSS from the webviewer's own Tailwind styles

### HTML Object Mapping

The agent generates HTML using FM object types mapped to web elements:

| FM Object Type | HTML Element | Notes |
|---|---|---|
| Edit Box | `<input>` or `<div>` | Styled with theme class, field name as placeholder |
| Text | `<span>` | Static label text |
| Button | `<button>` | Label text + optional inline SVG icon |
| Button Bar | `<div style="display:flex">` | Child buttons as flex items |
| Portal | `<div>` with `<table>` | Related TO name, sample rows |
| Rectangle | `<div>` | Background/border from theme |
| Container | `<div>` | For images/logos |
| Drop-down | `<select>` | Value list options if available |
| Drop-down Calendar | `<input type="date">` | Calendar picker |
| Radio Button Set | Radio inputs | Options from value list |

### Preview Container Structure

```html
<div class="fm-layout-preview" style="width: {layoutWidth}px; position: relative;">
  <div class="fm-part" data-part="top-navigation" style="height: 55px; position: relative;">
    <!-- Objects positioned absolutely within the part -->
  </div>
  <div class="fm-part" data-part="body" style="position: relative;">
    <!-- Body objects -->
  </div>
</div>
```

Each object uses `position: absolute` within its parent part, matching the FM layout's pixel-based coordinate system.

### Iteration Workflow

1. Agent generates HTML preview from the layout summary or design brief
2. Agent pushes to webviewer via `/webviewer/push`
3. Developer sees the preview in the Agent Output panel
4. Developer requests changes: "move the portal lower", "use the strong field style"
5. Agent modifies the HTML and pushes again
6. Repeat until the developer approves

---

## 4. Output Paths

Once the developer approves the preview, the agent asks: **"Do you want this as FM layout objects (XML2 paste) or as a Web Viewer app?"**

### Native FM Path (XML2)

The agent translates the approved HTML into `XML2` fmxmlsnippet format:

- HTML elements map back to FM `<LayoutObject>` types
- Pixel positions become `<Bounds top="..." left="..." bottom="..." right="..."/>`
- CSS class names become `<LocalCSS name="...">`
- Field references resolve from CONTEXT.json (TO ID + field ID)
- Script references for buttons resolve from CONTEXT.json
- The `XML2` clipboard class is used — `clipboard.py` auto-detects it from `<LayoutObject` elements

The developer creates the layout shell in FM (name, base TO), then pastes the XML2 objects in Layout Mode.

### Web Viewer Path

The agent produces a self-contained HTML file using the same theme CSS, plus FM bridge scripts:

- `FileMaker.PerformScript(scriptName, param)` — JS calls FM scripts
- `window.fmCallback(json)` — FM pushes data to the web viewer via `Perform JavaScript in Web Viewer`
- The HTML is set as the Web Viewer's URL or embedded via `Set Web Viewer` step

This path is recommended for new solutions, complex UI interactions, or solutions considering future migration off FileMaker. The theme CSS ensures visual consistency with native FM layouts.

---

## 5. Key Files

| File | Purpose |
|---|---|
| `agent/scripts/extract_theme.py` | Theme extraction: FM theme XML → CSS + manifest + classes |
| `agent/scripts/layout_to_summary.py` | Layout summarisation: verbose XML → compact JSON (78% reduction) |
| `agent/context/{solution}/theme.css` | Faithful FM CSS with shorthand consolidation |
| `agent/context/{solution}/theme-web.css` | Web-compatible CSS translation for preview rendering |
| `agent/context/{solution}/theme-manifest.json` | Theme metadata, color palette, layout builder constants, named styles |
| `agent/context/{solution}/theme-classes.json` | Style classes in use across layouts |
| `agent/context/{solution}/layouts/*.json` | Compact layout summaries |
| `webviewer/src/ui/AgentOutputPanel.tsx` | Preview renderer with Shadow DOM style isolation |
| `.claude/skills/layout-spec/SKILL.md` | Design conversation skill |
| `.claude/skills/layout-design/SKILL.md` | Preview-first generation skill |
| `.claude/skills/webviewer-build/SKILL.md` | Web Viewer app generation skill |

---

## 6. Constraints

- **The agent cannot create layout containers** — only the objects on them. The developer must create the layout in FM first (name, base TO, dimensions).
- **The agent must only suggest styles that exist in `theme-classes.json`** — creating new theme classes requires manual work in FM's Theme editor.
- **Layout XML in the fm-saxml docs is read-only** — never modified by the agent.
- **The preview is approximate** — FM renders objects through its own engine; the web preview uses CSS approximations. Pixel-perfect fidelity is not the goal; design decision fidelity is.
- **SVG icons in layout objects** are decoded from base64 for the preview but the agent does not generate new icon data — buttons in XML2 output use empty `<IconData>` unless the developer provides icon content.
