#!/usr/bin/env python3
"""
layout_to_summary.py — Extract a compact JSON summary of a FileMaker layout.

Strips the verbose Save-As-XML layout format (2000+ lines) down to a compact
JSON representation (~100-300 lines) containing only design-relevant data:
object types, positions, field bindings, styles, button wiring, and portal config.

Binary data (icons, images), hash attributes, numeric option bitfields, and
deeply nested formatting blocks are stripped. SVG icon data is replaced with
a reference note.

Reads the solution's fm-saxml docs (roadmap P2-09), through solution_context.py:
  - from the model: the layout's name, ID, width, base TO and theme, its parts, and
    each object's bounds, field binding, display style, value list, portal TO and
    rows, and button script;
  - from the layout's raw XML (fm-saxml's copy of the export): the object tree and
    order, styles and CSS visuals, text, labels, icons, parameters, tooltips,
    conditions and script triggers.

Usage:
  python3 agent/scripts/layout_to_summary.py -s "Invoice Solution" --docs-root <folder>      # all layouts
  python3 agent/scripts/layout_to_summary.py -s "Invoice Solution" --layout "Invoices Details"  # one, to stdout
  python3 agent/scripts/layout_to_summary.py -s exports/Invoices_docs --layout "layout:Invoices Details"
  python3 agent/scripts/layout_to_summary.py <layout_xml_path>                    # any layout XML file, XML only

--layout takes a name or docId; a name matching no layout exactly falls back to a
case-insensitive partial match. Without --layout, every layout is written to
agent/context/{solution}/layouts/ (-o names another folder). For a multi-file build,
add --file <FileMaker file>.
"""

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import solution_context as sc  # noqa: E402


def get_agent_root():
    """Return the absolute path to the agent/ directory."""
    return Path(__file__).resolve().parent.parent


def parse_bounds(obj_el):
    """Extract bounds as [top, left, bottom, right] from a LayoutObject element."""
    bounds = obj_el.find("Bounds")
    if bounds is None:
        return None
    return [
        int(bounds.get("top", 0)),
        int(bounds.get("left", 0)),
        int(bounds.get("bottom", 0)),
        int(bounds.get("right", 0)),
    ]


# <Field><Display Style> -> displayStyle. 3 and 7 were missing before the port (P2-09); the
# codes are fm-saxml's (confirmed on a FileMaker 26 export).
DISPLAY_STYLES = {"0": "editBox", "1": "dropDown", "2": "popUp", "3": "checkbox",
                  "4": "radioButtons", "6": "calendar", "7": "concealedEditBox"}


def parse_field(obj_el):
    """Extract field binding info: 'TO::FieldName'."""
    field = obj_el.find("Field")
    if field is None:
        return None
    ref = field.find("FieldReference")
    if ref is None:
        return None
    field_name = ref.get("name", "")
    field_id = ref.get("id", "")
    to_ref = ref.find("TableOccurrenceReference")
    to_name = to_ref.get("name", "") if to_ref is not None else ""

    result = {"field": f"{to_name}::{field_name}", "fieldId": int(field_id) if field_id else 0}

    # Display style
    display = field.find("Display")
    if display is not None:
        style_val = display.get("Style", "0")
        display_style = DISPLAY_STYLES.get(style_val, f"style_{style_val}")
        if display_style != "editBox":
            result["displayStyle"] = display_style

        # Value list reference
        vl = display.find("ValueListReference")
        if vl is not None:
            result["valueList"] = vl.get("name", "")

        # Placeholder
        ph = display.find("Placeholder")
        if ph is not None:
            calc = ph.find(".//Text")
            if calc is not None and calc.text:
                text = calc.text.strip().strip('"')
                if text:
                    result["placeholder"] = text

    return result


def _rgba_to_hex(rgba_str):
    """Convert 'rgba(R%, G%, B%, A)' to '#RRGGBB' or '#RRGGBBAA'.

    Handles both percentage values (0-100%) and 0-255 integer values.
    Returns None if parsing fails.
    """
    import re
    m = re.match(r'rgba?\(\s*([^,]+),\s*([^,]+),\s*([^,]+)(?:,\s*([^)]+))?\)', rgba_str.strip())
    if not m:
        return None
    try:
        vals = []
        for i in range(3):
            v = m.group(i + 1).strip()
            if v.endswith('%'):
                vals.append(int(float(v.rstrip('%')) * 255 / 100))
            else:
                vals.append(int(float(v)))
        alpha = float(m.group(4).strip()) if m.group(4) else 1.0
        if alpha < 1.0:
            return "#{:02X}{:02X}{:02X}{:02X}".format(vals[0], vals[1], vals[2], int(alpha * 255))
        return "#{:02X}{:02X}{:02X}".format(vals[0], vals[1], vals[2])
    except (ValueError, TypeError):
        return None


def _extract_css_visuals(css_text):
    """Extract key visual CSS properties from a LocalCSS CDATA block.

    Returns a dict with only the properties that carry visual information:
    background-color, color (text), border-radius, font-size, font-family,
    background-image (gradients). Skips empty/zero values.
    """
    import re
    if not css_text:
        return {}

    visuals = {}

    # Background color
    m = re.search(r'background-color:\s*(rgba?\([^)]+\))', css_text)
    if m:
        hex_val = _rgba_to_hex(m.group(1))
        if hex_val and hex_val not in ("#000000FF", "#00000000"):
            # Skip fully transparent (not useful) but keep others
            if not hex_val.endswith("00"):  # skip alpha=0
                visuals["bgColor"] = hex_val

    # Background gradient
    m = re.search(r'background-image:\s*-webkit-gradient\([^)]+from\((rgba?\([^)]+\))\)[^)]*to\((rgba?\([^)]+\))\)', css_text)
    if m:
        from_hex = _rgba_to_hex(m.group(1))
        to_hex = _rgba_to_hex(m.group(2))
        if from_hex and to_hex:
            visuals["bgGradient"] = [from_hex, to_hex]

    # Text color (avoid matching background-color)
    for cm in re.finditer(r'(?<!background-)color:\s*(rgba?\([^)]+\))', css_text):
        hex_val = _rgba_to_hex(cm.group(1))
        if hex_val and hex_val not in ("#00000000",):
            visuals["textColor"] = hex_val
            break

    # Border radius
    m = re.search(r'border-(?:top-left-)?radius:\s*([0-9.]+(?:pt|px|em))', css_text)
    if m and float(re.match(r'[0-9.]+', m.group(1)).group()) > 0:
        visuals["borderRadius"] = m.group(1)

    # Font size
    m = re.search(r'font-size:\s*([0-9.]+(?:pt|px|em))', css_text)
    if m:
        visuals["fontSize"] = m.group(1)

    # FM font family
    m = re.search(r'-fm-font-family\(([^,)]+)', css_text)
    if m:
        visuals["fontFamily"] = m.group(1)

    return visuals


def _describe_icon_svg(icon_el):
    """Extract a brief description from an IconData element's SVG content.

    Decodes the base64 SVG and extracts the shape types to produce a compact
    description like 'svg:24x24 path+rect+polygon' (the SVG primitives used).
    Returns None if no SVG data found.
    """
    import base64
    import re
    if icon_el is None:
        return None

    binary = icon_el.find("BinaryData")
    if binary is None:
        return None

    for stream in binary.iter("Stream"):
        if stream.get("name", "").strip() == "SVG" and stream.get("type") == "Base64":
            try:
                svg_text = base64.b64decode(stream.text.strip()).decode("utf-8", errors="replace")
            except Exception:
                return None

            # Extract viewBox dimensions
            vb = re.search(r'viewBox="([^"]+)"', svg_text)
            size = ""
            if vb:
                parts = vb.group(1).split()
                if len(parts) == 4:
                    size = f"{parts[2]}x{parts[3]}"

            # Extract shape primitives used
            shapes = set(re.findall(r'<(path|rect|circle|ellipse|polygon|polyline|line)\b', svg_text))
            if shapes:
                return f"svg:{size} {'+'.join(sorted(shapes))}" if size else f"svg:{'+'.join(sorted(shapes))}"
            return f"svg:{size}" if size else "svg"

    return None


def parse_style(obj_el):
    """Extract the LocalCSS class name, display name, and key visual properties.

    Returns a dict with 'class', optionally 'displayName', and optionally
    'visuals' (extracted CSS properties like bgColor, textColor, fontSize).
    Returns just the class string when only the class name is available.
    """
    css = obj_el.find("LocalCSS")
    if css is None:
        return None
    name = css.get("name", "")
    display_name = css.get("displayName", "")
    if not name and not display_name:
        return None

    # Extract visual CSS properties from the CDATA content
    css_text = css.text or ""
    visuals = _extract_css_visuals(css_text)

    # Also check for anonymous inline CSS blocks (children without names)
    # These appear on child elements like buttons/segments
    for child_css in obj_el.iter("LocalCSS"):
        if child_css is not css:
            child_text = child_css.text or ""
            child_visuals = _extract_css_visuals(child_text)
            # Only add if the main block didn't already have it
            for k, v in child_visuals.items():
                if k not in visuals:
                    visuals[k] = v

    if not name and not display_name and not visuals:
        return None

    # Return minimal format when only class name is available
    if name and not display_name and not visuals:
        return name

    result = {}
    if name:
        result["class"] = name
    if display_name:
        result["displayName"] = display_name
    if visuals:
        result["visuals"] = visuals
    return result if result else name


def parse_text_content(obj_el):
    """Extract text content from StyledText or Text elements."""
    # StyledText path (static text labels)
    for styled in obj_el.iter("StyledText"):
        data = styled.find("Data")
        if data is not None and data.text:
            return data.text.strip()
    # Text path
    for text in obj_el.iter("Text"):
        if text.text and "CDATA" not in (text.text or ""):
            return text.text.strip()
    return None


def parse_button(obj_el):
    """Extract button info: label, script action, tooltip, has icon."""
    btn = obj_el.find("Button")
    if btn is None:
        return None

    result = {}

    # Label text
    label = btn.find("Label")
    if label is not None:
        text = parse_text_content(label)
        if text:
            result["label"] = text

    # Has icon? Extract SVG description if available.
    icon = btn.find("IconData")
    if icon is not None:
        icon_type = icon.get("type", "0")
        if icon_type != "0":
            result["hasIcon"] = True
            icon_desc = _describe_icon_svg(icon)
            if icon_desc:
                result["iconDesc"] = icon_desc

    # Script action
    action = btn.find("action")
    if action is not None:
        script_ref = action.find("ScriptReference")
        if script_ref is not None:
            result["script"] = script_ref.get("name", "")
            result["scriptId"] = int(script_ref.get("id", 0))
        # Script parameter
        calc = action.find(".//Text")
        if calc is not None and calc.text:
            param = calc.text.strip().strip('"')
            if param:
                result["param"] = param

    # Tooltip
    tooltip = obj_el.find("Tooltip")
    if tooltip is not None:
        calc = tooltip.find(".//Text")
        if calc is not None and calc.text:
            result["tooltip"] = calc.text.strip().strip('"')

    return result


def parse_portal(obj_el, model=None):
    """Extract portal configuration."""
    portal = obj_el.find("Portal")
    if portal is None:
        return None

    result = {}

    # Related TO
    to_ref = portal.find("TableOccurrenceReference")
    if to_ref is not None:
        result["relatedTO"] = to_ref.get("name", "")

    # Row count
    opts = portal.find("Options")
    if opts is not None:
        show = opts.get("show", "")
        if show:
            result["visibleRows"] = int(show)

    # Portal fields (nested objects)
    obj_list = portal.find("ObjectList")
    if obj_list is not None:
        portal_objects = []
        for child in obj_list:
            child_summary = parse_layout_object(child, model)
            if child_summary:
                portal_objects.append(child_summary)
        if portal_objects:
            result["objects"] = portal_objects

    return result


def parse_button_bar(obj_el, model=None):
    """Extract button bar with child buttons."""
    bar = obj_el.find("ButtonBar")
    if bar is None:
        return None

    result = {}

    # Active segment
    select = bar.find("Select")
    if select is not None:
        sel_id = select.find("id")
        if sel_id is not None and sel_id.text:
            result["activeSegment"] = int(sel_id.text)

    # Child buttons
    obj_list = bar.find("ObjectList")
    if obj_list is not None:
        buttons = []
        for child in obj_list:
            child_summary = parse_layout_object(child, model)
            if child_summary:
                buttons.append(child_summary)
        if buttons:
            result["buttons"] = buttons

    return result


def parse_conditions(obj_el):
    """Extract conditional visibility/formatting info (compact)."""
    conds = obj_el.find("Conditions")
    if conds is None:
        return None

    result = {}

    # Hide condition
    hide = conds.find("Hide")
    if hide is not None:
        calc = hide.find(".//Text")
        if calc is not None and calc.text:
            result["hideWhen"] = calc.text.strip()
        find_mode = hide.get("findMode")
        if find_mode == "True":
            result["hideInFind"] = True

    # Conditional formatting (just count, not full details)
    formatting = conds.find("Formatting")
    if formatting is not None:
        count = formatting.get("membercount", "0")
        if int(count) > 0:
            result["conditionalFormats"] = int(count)

    return result if result else None


def parse_triggers(el):
    """Extract script triggers directly attached to a Layout or LayoutObject element.

    Returns a list of {event, script, scriptId} dicts, or None. Captures both
    layout-level triggers (OnRecordLoad, OnLayoutKeystroke, …) and object-level
    triggers (OnObjectEnter, OnObjectSave, …). A script wired only as a trigger
    has no button/Perform Script caller — without recording these edges it looks
    orphaned and gets false-flagged as dead.
    """
    # <ScriptTriggers> can sit at varying depths inside an object's subtree
    # (directly under a LayoutObject, or nested beside a field's
    # ExtendedAttributes). Walk this element's own subtree but stop at nested
    # LayoutObject boundaries so each object claims only its own triggers — no
    # double-counting, none missed.
    result = []

    def _collect(node):
        for child in node:
            if child.tag == "LayoutObject":
                continue  # belongs to a nested object — parsed separately
            if child.tag == "ScriptTriggers":
                for trig in child.findall("ScriptTrigger"):
                    script_ref = trig.find("ScriptReference")
                    if script_ref is None:
                        continue
                    name = script_ref.get("name", "")
                    if not name:
                        continue
                    result.append({
                        "event": trig.get("action", ""),
                        "script": name,
                        "scriptId": int(script_ref.get("id", 0)),
                    })
            else:
                _collect(child)

    _collect(el)
    return result if result else None


def parse_action_script(el):
    """Direct button-action script wired into this object.

    Covers every button variant — classic <Button>, <GroupedButton> ("Grouped
    Button" / Popover Button), etc. — whose action runs a script *directly*
    (<action><ScriptReference>), as opposed to a Perform Script step or a script
    trigger. parse_button only handles the classic <Button> element, so without
    this, Grouped/Popover button scripts never reach the summary and their
    target scripts get false-flagged as dead. Walks this element's own subtree
    but stops at nested LayoutObject boundaries so it never steals a child
    object's action. Returns {script, scriptId} or None.
    """
    result = []

    def _find(node):
        for child in node:
            if result:
                return
            if child.tag == "LayoutObject":
                continue  # belongs to a nested object — parsed separately
            if child.tag == "action":
                script_ref = child.find("ScriptReference")
                if script_ref is not None and script_ref.get("name"):
                    result.append({
                        "script": script_ref.get("name"),
                        "scriptId": int(script_ref.get("id", 0)),
                    })
                    return
            else:
                _find(child)

    _find(el)
    return result[0] if result else None


def collect_child_objects(el):
    """Nested LayoutObjects belonging directly to this object.

    FileMaker nests objects inside many container types — Group, Popover Button,
    Tab/Slide panels, GroupedButton wrappers — not just Portals and Button Bars.
    Walk this element's subtree and return the first-level nested LayoutObjects
    (those reachable without crossing a deeper LayoutObject boundary). Without
    this, fields/buttons/triggers inside groups are silently dropped from the
    summary — and therefore invisible to the cross-reference index, producing
    false "dead object" verdicts.
    """
    found = []

    def _collect(node):
        for child in node:
            if child.tag == "LayoutObject":
                found.append(child)
            else:
                _collect(child)

    _collect(el)
    return found


def parse_layout_object(obj_el, model=None):
    """Parse a single LayoutObject element into a compact dict.

    With ``model`` (a LayoutModel), the keys the fm-saxml model holds come from it.
    """
    obj_type = obj_el.get("type", "Unknown")
    obj_name = obj_el.get("name", "")
    obj_key = obj_el.get("key", "")

    summary = {"type": obj_type}
    if obj_name:
        summary["name"] = obj_name
    if obj_key:
        summary["key"] = int(obj_key)

    # Bounds
    bounds = parse_bounds(obj_el)
    if bounds:
        summary["bounds"] = bounds

    # Style — may be a string (class only) or dict (class + displayName + visuals)
    style = parse_style(obj_el)
    if style:
        if isinstance(style, dict):
            if style.get("class"):
                summary["style"] = style["class"]
            if style.get("displayName"):
                summary["styleName"] = style["displayName"]
            if style.get("visuals"):
                summary["visuals"] = style["visuals"]
        else:
            summary["style"] = style

    # Type-specific content
    if obj_type in ("Edit Box", "Drop-down List", "Drop-down Calendar",
                     "Radio Button Set", "Checkbox Set", "Pop-up Menu", "Container"):
        field_info = parse_field(obj_el)
        if field_info:
            summary.update(field_info)

    elif obj_type == "Text":
        text = parse_text_content(obj_el)
        if text:
            summary["text"] = text

    elif obj_type == "Button":
        btn_info = parse_button(obj_el)
        if btn_info:
            summary.update(btn_info)

    elif obj_type == "Button Bar":
        bar_info = parse_button_bar(obj_el, model)
        if bar_info:
            summary.update(bar_info)

    elif obj_type == "Portal":
        portal_info = parse_portal(obj_el, model)
        if portal_info:
            summary.update(portal_info)

    # Also check for field on types that might have it generically
    if "field" not in summary:
        field_info = parse_field(obj_el)
        if field_info:
            summary.update(field_info)

    # Conditions (compact)
    conditions = parse_conditions(obj_el)
    if conditions:
        summary["conditions"] = conditions

    # Direct button-action script for button variants parse_button doesn't
    # handle (Grouped Button, Popover Button, …). Guarded so classic <Button>
    # objects — already handled above — aren't double-counted.
    if "script" not in summary:
        action_script = parse_action_script(obj_el)
        if action_script:
            summary.update(action_script)

    # Object-level script triggers (OnObjectEnter, OnObjectSave, …)
    triggers = parse_triggers(obj_el)
    if triggers:
        summary["triggers"] = triggers

    # Nested objects inside container types not handled above (Group, Popover
    # Button, Tab/Slide panels, GroupedButton wrappers). Portal and Button Bar
    # manage their own children, so skip them here to avoid double-counting.
    if obj_type not in ("Portal", "Button Bar") and "objects" not in summary:
        children = collect_child_objects(obj_el)
        if children:
            summary["objects"] = [parse_layout_object(c, model) for c in children]

    if model is not None:
        model.apply(obj_el, summary)
    return summary


def parse_part(part_el, model=None):
    """Parse a layout Part element."""
    defn = part_el.find("Definition")
    if defn is None:
        return None

    part_type = defn.get("type", "Unknown")
    size = int(defn.get("size", 0))

    result = {
        "type": part_type,
        "height": size,
    }

    # Part style
    css = defn.find("LocalCSS")
    if css is not None:
        name = css.get("name", "")
        display_name = css.get("displayName", "")
        if name:
            result["style"] = name
        if display_name:
            result["styleName"] = display_name

    # Objects in this part
    obj_list = part_el.find("ObjectList")
    if obj_list is not None:
        objects = []
        for obj_el in obj_list:
            obj_summary = parse_layout_object(obj_el, model)
            if obj_summary:
                objects.append(obj_summary)
        if objects:
            result["objects"] = objects

    return result


def parse_layout(root, model=None):
    """Summarise a <Layout> element; with ``model``, model-backed keys come from the model."""
    summary = {
        "layout": root.get("name", ""),
        "id": int(root.get("id", 0)),
        "width": int(root.get("width", 0)),
    }

    # Table occurrence
    to_ref = root.find("TableOccurrenceReference")
    if to_ref is not None:
        summary["table"] = to_ref.get("name", "")

    # Theme
    theme_ref = root.find("LayoutThemeReference")
    if theme_ref is not None:
        summary["theme"] = theme_ref.get("name", "")

    # Parts
    parts_list = root.find("PartsList")
    if parts_list is not None:
        parts = []
        for part_el in parts_list:
            part_summary = parse_part(part_el, model)
            if part_summary:
                parts.append(part_summary)
        summary["parts"] = parts

    # Layout-level script triggers (OnRecordLoad, OnLayoutKeystroke, …)
    triggers = parse_triggers(root)
    if triggers:
        summary["triggers"] = triggers

    if model is not None:
        model.apply_layout(summary)
    return summary


class LayoutModel:
    """The fm-saxml model of one layout, for the keys it holds (P2-09).

    The summary is built by walking the layout's raw XML (order, nesting and the keys only
    the XML has); apply() and apply_layout() then replace the model-backed keys with the
    model's values. A key is only replaced, never added, so the key order stays the XML's;
    where the model can't resolve a reference (another file, a deleted object), the
    XML's value stays.
    """

    # fm-saxml's display style names -> this summary's names
    DISPLAY_NAMES = {"editBox": "editBox", "dropDownList": "dropDown", "popUpMenu": "popUp",
                     "checkboxSet": "checkbox", "radioButtonSet": "radioButtons",
                     "dropDownCalendar": "calendar", "concealedEditBox": "concealedEditBox"}

    def __init__(self, ctx, layout):
        self.ctx = ctx
        self.layout = layout
        self.objects = {}
        for doc_id in layout.layout_objects or []:
            obj = self._get(doc_id)
            if obj is not None and obj.object_id is not None:
                self.objects[str(obj.object_id)] = obj

    def _get(self, doc_id):
        if not doc_id:
            return None
        try:
            return self.ctx.get(doc_id)
        except KeyError:
            return None

    def _name(self, doc_id):
        e = self._get(doc_id)
        return getattr(e, "name", None) if e is not None else None

    def apply_layout(self, summary):
        lay = self.layout
        summary["layout"] = lay.name
        if lay.fmp_id is not None:
            summary["id"] = int(lay.fmp_id)
        if lay.width is not None:
            summary["width"] = int(lay.width)
        table = self._name(lay.base_table_occurrence_doc_id)
        if "table" in summary and table:
            summary["table"] = table
        if "theme" in summary and lay.theme:
            summary["theme"] = lay.theme
        parts = summary.get("parts") or []
        if len(parts) == len(lay.parts or []):
            for part, mp in zip(parts, lay.parts):
                part["type"] = mp.type or part["type"]
                if mp.height is not None:
                    part["height"] = int(mp.height)

    def _display_style(self, obj):
        code = getattr(obj, "display_style_code", None)
        if code is not None and str(code) in DISPLAY_STYLES:
            return DISPLAY_STYLES[str(code)]
        return self.DISPLAY_NAMES.get(obj.display_style or "")

    def apply(self, obj_el, summary):
        obj = self.objects.get(obj_el.get("id", ""))
        if obj is None:
            return
        b = obj.bounds
        if "bounds" in summary and b is not None:
            summary["bounds"] = [int(b.top), int(b.left), int(b.bottom), int(b.right)]
        if "field" in summary and obj.field_doc_id:
            field, to = self._get(obj.field_doc_id), self._get(obj.table_occurrence_doc_id)
            if field is not None and to is not None:
                summary["field"] = f"{to.name}::{field.name}"
                if field.fmp_id is not None:
                    summary["fieldId"] = int(field.fmp_id)
            style = self._display_style(obj)
            if style == "editBox":
                summary.pop("displayStyle", None)
            elif style and "displayStyle" in summary:
                summary["displayStyle"] = style
        if "valueList" in summary:
            summary["valueList"] = self._name(obj.value_list_doc_id) or summary["valueList"]
        if "relatedTO" in summary:
            summary["relatedTO"] = self._name(obj.table_occurrence_doc_id) or summary["relatedTO"]
        if "visibleRows" in summary and obj.portal is not None and obj.portal.rows:
            summary["visibleRows"] = int(obj.portal.rows)
        if "script" in summary and obj.button_script_doc_id:
            script = self._get(obj.button_script_doc_id)
            if script is not None:
                summary["script"] = script.name
                if "scriptId" in summary and script.fmp_id is not None:
                    summary["scriptId"] = int(script.fmp_id)


def _order(layout):
    return (layout.name.casefold(), int(layout.fmp_id or 0))


def select_layouts(ctx, key=None):
    """Every layout (no key), or the one whose name or docId is ``key``.

    A name matching no layout exactly falls back to a case-insensitive partial match,
    as before the port; several layouts with the exact name is an error listing them.
    """
    if not key:
        return sorted(ctx.layouts.values(), key=_order)
    try:
        return [ctx.pick("layout", key)]
    except sc.SolutionContextError:
        if len(ctx.find("layout", key)) > 1:
            raise
        partial = [lay for lay in ctx.layouts.values() if key.casefold() in (lay.name or "").casefold()]
        if not partial:
            raise
        return sorted(partial, key=_order)


def summarise(ctx, layout):
    """(summary, raw XML size in bytes) for one layout of the solution."""
    raw = ctx.raw_xml(layout.doc_id)
    return parse_layout(ET.fromstring(raw), LayoutModel(ctx, layout)), len(raw.encode("utf-8"))


def _dump(summary, indent):
    return json.dumps(summary, indent=indent, ensure_ascii=False)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Extract compact JSON summaries of FileMaker layouts from a solution's fm-saxml docs."
    )
    parser.add_argument("layout_xml", nargs="?",
                        help="a layout XML file, summarised from the XML alone (no -s needed)")
    sc.add_arguments(parser, solution_required=False)
    parser.add_argument("--layout", help="layout name or docId (default: every layout)")
    parser.add_argument("-o", "--output",
                        help="with a layout XML file: output file; with -s: output folder "
                             "(default: agent/context/{solution}/layouts/)")
    parser.add_argument("--print", action="store_true", dest="to_stdout",
                        help="with -s: print the JSON instead of writing files")
    parser.add_argument("--compact", action="store_true", help="Minimal JSON (no indentation)")

    args = parser.parse_args(argv)
    indent = None if args.compact else 2

    # Single file mode: any layout XML, no model
    if args.layout_xml:
        xml_path = Path(args.layout_xml)
        if not xml_path.exists():
            print(f"Error: File not found: {xml_path}", file=sys.stderr)
            return 1
        output = _dump(parse_layout(ET.parse(xml_path).getroot()), indent)
        if args.output:
            Path(args.output).parent.mkdir(parents=True, exist_ok=True)
            with open(args.output, "w", encoding="utf-8", newline="\n") as f:
                f.write(output + "\n")
            print(f"Written to {args.output}")
        else:
            print(output)
        return 0

    if not args.solution:
        parser.error("pass -s/--solution (with --docs-root or $FM_SAXML_DOCS_ROOT), or a layout XML file")
    try:
        ctx = sc.from_args(args)
        layouts = select_layouts(ctx, args.layout)
        if not layouts:
            print(f"No layouts in {ctx.name} ({ctx.path})", file=sys.stderr)
            return 1
        summaries = [(lay, *summarise(ctx, lay)) for lay in layouts]
    except sc.SolutionContextError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    if args.to_stdout:
        for _, summary, _ in summaries:
            print(_dump(summary, indent))
        return 0

    output_dir = Path(args.output) if args.output else get_agent_root() / "context" / ctx.name / "layouts"
    output_dir.mkdir(parents=True, exist_ok=True)

    total_xml_bytes = 0
    total_json_bytes = 0
    for _, summary, xml_size in summaries:
        layout_name = summary.get("layout", "unknown")
        layout_id = summary.get("id", 0)
        output = _dump(summary, indent)
        with open(output_dir / f"{layout_name} - ID {layout_id}.json", "w", encoding="utf-8", newline="\n") as f:
            f.write(output + "\n")

        json_size = len(output.encode("utf-8"))
        total_xml_bytes += xml_size
        total_json_bytes += json_size

        obj_count = sum(len(p.get("objects", [])) for p in summary.get("parts", []))
        print(f"  {layout_name} (ID {layout_id}): {xml_size:,} -> {json_size:,} bytes ({obj_count} objects)")

    pct = (1 - total_json_bytes / total_xml_bytes) * 100 if total_xml_bytes else 0
    print(f"\nSummarised {len(summaries)} layout(s) for '{ctx.name}'")
    print(f"  XML total:  {total_xml_bytes:,} bytes")
    print(f"  JSON total: {total_json_bytes:,} bytes ({pct:.0f}% reduction)")
    print(f"  Output: {output_dir}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
