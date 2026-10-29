#!/usr/bin/env python3
"""
Extract FileMaker theme data into web-usable CSS and a JSON manifest.

Reads the solution's fm-saxml docs (roadmap P2-10), through solution_context.py: the
theme's metadata (names, ID, base theme, color palette, layout builder constants,
charting, named styles) from the model, its CSS from the theme's raw XML, and the
style classes from every layout's raw XML. It produces:
  - theme.css           — full CSS from the theme's CDATA block
  - theme-web.css       — web-compatible translation for preview rendering
  - theme-manifest.json — structured metadata (palette, layout builder, named styles)
  - theme-classes.json  — named style classes found across all layouts

Output is written to agent/context/{solution}/ (-o names another folder).

Usage:
  python3 agent/scripts/extract_theme.py -s "Invoice Solution" --docs-root <folder>   # default theme
  python3 agent/scripts/extract_theme.py -s exports/Invoices_docs --theme "Apex Blue"
  python3 agent/scripts/extract_theme.py -s "Invoice Solution" --list                 # list the themes

-s also takes the docs folder; --file picks a file of a multi-file build.
"""

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import solution_context as sc  # noqa: E402


def get_agent_root():
    """Return the absolute path to the agent/ directory."""
    script_dir = Path(__file__).resolve().parent
    return script_dir.parent


def list_themes(ctx):
    """Print the solution's themes, the default marked."""
    print(f"{ctx.name} ({ctx.path}):")
    for theme in sorted(ctx.themes.values(), key=_theme_order):
        default = " (default)" if theme.default_theme else ""
        print(f"  - {theme.display_name or theme.name} (ID {theme.fmp_id}, {theme.doc_id}){default}")


def _theme_order(theme):
    return ((theme.display_name or theme.name or "").casefold(), int(theme.fmp_id or 0))


def pick_theme(ctx, key=None):
    """The theme to extract: ``key`` (display name, internal name or docId), else the default
    theme, else the first by display name. None when the file has no themes."""
    themes = ctx.themes
    if key:
        if key in themes:
            return themes[key]
        hits = [t for t in themes.values()
                if key.casefold() in ((t.display_name or "").casefold(), (t.name or "").casefold())]
        if len(hits) == 1:
            return hits[0]
        listed = "\n".join(f"  {t.display_name} ({t.doc_id})" for t in sorted(themes.values(), key=_theme_order))
        raise sc.SolutionContextError(f"{'Several themes' if hits else 'No theme'} {key!r} in {ctx.name}; "
                                      f"its themes:\n{listed}")
    if not themes:
        return None
    for theme in themes.values():
        if theme.default_theme:
            return theme
    return sorted(themes.values(), key=_theme_order)[0]


def extract_css(theme_root):
    """Extract the CSS CDATA content from the theme XML."""
    css_elem = theme_root.find("CSS")
    if css_elem is None or css_elem.text is None:
        return ""
    return css_elem.text


def parse_named_styles(metadata_elem):
    """Parse the <namedstyles> section into a list of dicts."""
    ns_elem = metadata_elem.find("namedstyles")
    if ns_elem is None:
        return []

    styles = []
    for child in ns_elem:
        styles.append({
            "name": child.tag,
            "displayName": child.text or ""
        })
    return styles


def parse_layout_builder(metadata_elem):
    """Parse the <layoutbuilder> section into a dict."""
    lb_elem = metadata_elem.find("layoutbuilder")
    if lb_elem is None:
        return {}

    result = {}
    for child in lb_elem:
        # Convert numeric strings to int
        try:
            result[child.tag] = int(child.text)
        except (ValueError, TypeError):
            result[child.tag] = child.text or ""
    return result


def parse_color_palette(metadata_elem):
    """Parse the <colorpalette> section into a dict."""
    cp_elem = metadata_elem.find("colorpalette")
    if cp_elem is None:
        return {}

    result = {}
    for child in cp_elem:
        result[child.tag] = child.text or ""
    return result


def parse_charting(metadata_elem):
    """Parse the <charting> section."""
    ch_elem = metadata_elem.find("charting")
    if ch_elem is None:
        return {}
    result = {}
    for child in ch_elem:
        result[child.tag] = child.text or ""
    return result


def extract_object_types(css_text):
    """Extract unique FM object type selectors from the CSS.

    Selectors look like: object_type:state .part
    or: object_type.style_name:state .part
    We want just the base object type names.
    """
    # Match the start of each CSS rule: word characters before : or .
    pattern = re.compile(r'^([a-z_]+)[\.:][^\s]*\s*\.', re.MULTILINE)
    types = set()
    for m in pattern.finditer(css_text):
        types.add(m.group(1))
    return sorted(types)


def extract_css_for_style(css_text, style_name):
    """Extract all CSS rules that reference a given named style.

    Named styles appear as: object_type.style_name:state .part { ... }
    """
    # Find all rule blocks that contain .style_name
    pattern = re.compile(
        r'(^[^\s{]*\.' + re.escape(style_name) + r'[^\{]*\{[^}]*\})',
        re.MULTILINE
    )
    matches = pattern.findall(css_text)
    if matches:
        return "\n".join(matches)
    return ""


def add_fm_property_comments(css_text):
    """Add comments noting FM-specific CSS properties and values."""
    lines = css_text.split("\n")
    result = []
    for line in lines:
        stripped = line.strip()
        # Annotate any line containing -fm- as a property name or value
        if "-fm-" in stripped and "/* FM-specific */" not in line:
            result.append(line + "  /* FM-specific */")
        else:
            result.append(line)
    return "\n".join(result)


# ---------------------------------------------------------------------------
# FM-to-web CSS translation
# ---------------------------------------------------------------------------

# Inline SVG icons matching FileMaker's built-in control glyphs.
# Each is a minimal SVG at 16x16 viewBox, using currentColor for fill
# so -fm-icon-color applies via the parent element's CSS `color` property.
_FM_ICONS = {
    "radio": (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16" width="1em" height="1em">'
        '<circle cx="8" cy="8" r="4" fill="currentColor"/>'
        '</svg>'
    ),
    "modern-check": (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16" width="1em" height="1em">'
        '<path d="M3.5 8.5L6.5 11.5L12.5 4.5" fill="none" stroke="currentColor" '
        'stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>'
        '</svg>'
    ),
    "up-arrow": (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16" width="1em" height="1em">'
        '<path d="M4 10L8 6L12 10" fill="none" stroke="currentColor" '
        'stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>'
        '</svg>'
    ),
    "down-arrow": (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16" width="1em" height="1em">'
        '<path d="M4 6L8 10L12 6" fill="none" stroke="currentColor" '
        'stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>'
        '</svg>'
    ),
    "calendar": (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16" width="1em" height="1em">'
        '<rect x="2" y="3" width="12" height="11" rx="1" fill="none" stroke="currentColor" stroke-width="1.2"/>'
        '<line x1="2" y1="6" x2="14" y2="6" stroke="currentColor" stroke-width="1.2"/>'
        '<line x1="5" y1="1.5" x2="5" y2="4" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/>'
        '<line x1="11" y1="1.5" x2="11" y2="4" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/>'
        '</svg>'
    ),
}

# Regex patterns for FM-specific property translation
_RE_FM_FONT = re.compile(r'-fm-font-family\(([\w-]+)(?:,[\w-]+)?\)')
_RE_FM_ICON_PROP = re.compile(r'^\s*-fm-icon\s*:\s*(.+?)\s*;')
_RE_FM_ICON_COLOR = re.compile(r'^\s*-fm-icon-color\s*:\s*(.+?)\s*;')
_RE_FM_ICON_PADDING = re.compile(r'^\s*-fm-icon-padding\s*:\s*(.+?)\s*;')
_RE_FM_TEXT_VALIGN = re.compile(r'^\s*-fm-text-vertical-align\s*:\s*(.+?)\s*;')
_RE_FM_TAB_SPACING = re.compile(r'^\s*-fm-tab-spacing\s*:\s*(.+?)\s*;')
_RE_FM_ALT_BG = re.compile(r'^\s*-fm-(portal|body)-alt-background\s*:\s*(.+?)\s*;')
_RE_FM_CURRENT_ROW = re.compile(r'^\s*-fm-use-(portal|body)-current-row-style\s*:\s*(.+?)\s*;')


def translate_fm_to_web(css_text):
    """Translate FM-specific CSS properties to web equivalents.

    Produces a web-compatible CSS file (theme-web.css) suitable for
    rendering FM layout previews in a browser or WebViewer.

    Translation rules:
      -fm-font-family(Name,Name)     → font-family: 'Name', sans-serif
      -fm-icon: <name>               → --fm-icon-svg: url("data:...") (CSS custom property)
      -fm-icon-color: <rgba>         → color: <rgba> (on .icon elements, drives currentColor in SVG)
      -fm-icon-padding: <val>        → padding: <val>
      -fm-text-vertical-align: <val> → display: flex; align-items: <mapped>
      -fm-tab-spacing: <val>         → gap: <val>
      -fm-portal-alt-background      → (comment: apply via :nth-child(even) in preview)
      -fm-use-portal-current-row-style → (comment: apply via :hover in preview)
    """
    lines = css_text.split("\n")
    result = []
    icon_css_injected = False

    for line in lines:
        stripped = line.strip()
        indent = line[:len(line) - len(line.lstrip())]

        # Skip FM-specific comment annotations (they were for theme.css, not theme-web.css)
        clean = re.sub(r'\s*/\* FM-specific \*/', '', line)
        stripped_clean = clean.strip()

        # -fm-font-family(Name-Variant,Name-Variant) → font-family + font-weight
        if '-fm-font-family' in stripped_clean:
            m = _RE_FM_FONT.search(stripped_clean)
            if m:
                raw_name = m.group(1)
                # Parse variant suffix: HelveticaNeue-Bold → family=HelveticaNeue, weight=bold
                font_family = raw_name
                font_weight = None
                if '-' in raw_name:
                    parts = raw_name.rsplit('-', 1)
                    variant = parts[1].lower()
                    weight_map = {
                        'bold': 'bold', 'light': '300', 'medium': '500',
                        'thin': '100', 'ultralight': '200', 'semibold': '600',
                        'heavy': '800', 'black': '900', 'italic': None,
                    }
                    if variant in weight_map:
                        font_family = parts[0]
                        font_weight = weight_map[variant]
                # Emit font-family (insert spaces in camelCase: HelveticaNeue → Helvetica Neue)
                display_name = re.sub(r'([a-z])([A-Z])', r'\1 \2', font_family)
                result.append(f"{indent}font-family: '{display_name}', 'Helvetica Neue', Helvetica, sans-serif;")
                if font_weight:
                    result.append(f"{indent}font-weight: {font_weight};")
                if variant == 'italic' if '-' in raw_name else False:
                    result.append(f"{indent}font-style: italic;")
                continue

        # -fm-icon: <name> → CSS custom property with data URI SVG
        m = _RE_FM_ICON_PROP.match(stripped_clean)
        if m:
            icon_name = m.group(1).strip()
            svg = _FM_ICONS.get(icon_name)
            if svg:
                import base64
                b64 = base64.b64encode(svg.encode()).decode()
                result.append(f'{indent}--fm-icon-svg: url("data:image/svg+xml;base64,{b64}");')
                result.append(f'{indent}content: "";')
                result.append(f'{indent}display: inline-block;')
                result.append(f'{indent}width: 1em;')
                result.append(f'{indent}height: 1em;')
                result.append(f'{indent}mask-image: var(--fm-icon-svg);')
                result.append(f'{indent}-webkit-mask-image: var(--fm-icon-svg);')
                result.append(f'{indent}mask-size: contain;')
                result.append(f'{indent}-webkit-mask-size: contain;')
                result.append(f'{indent}background-color: currentColor;')
            else:
                result.append(f"{indent}/* unsupported -fm-icon: {icon_name} */")
            continue

        # -fm-icon-color → color (drives currentColor in SVG mask)
        m = _RE_FM_ICON_COLOR.match(stripped_clean)
        if m:
            result.append(f"{indent}color: {m.group(1)};")
            continue

        # -fm-icon-padding → padding
        m = _RE_FM_ICON_PADDING.match(stripped_clean)
        if m:
            result.append(f"{indent}padding: {m.group(1)};")
            continue

        # -fm-text-vertical-align → flexbox align-items
        m = _RE_FM_TEXT_VALIGN.match(stripped_clean)
        if m:
            val = m.group(1).strip()
            flex_val = {"center": "center", "top": "flex-start", "bottom": "flex-end"}.get(val, val)
            result.append(f"{indent}display: flex;")
            result.append(f"{indent}align-items: {flex_val};")
            continue

        # -fm-tab-spacing → gap
        m = _RE_FM_TAB_SPACING.match(stripped_clean)
        if m:
            result.append(f"{indent}gap: {m.group(1)};")
            continue

        # -fm-portal-alt-background / -fm-body-alt-background → comment with guidance
        m = _RE_FM_ALT_BG.match(stripped_clean)
        if m:
            context = m.group(1)  # "portal" or "body"
            enabled = m.group(2).strip().lower()
            if enabled == "true":
                result.append(f"{indent}/* alternating row background: apply :nth-child(even) background in {context} */")
            continue

        # -fm-use-portal-current-row-style / -fm-use-body-current-row-style → comment
        m = _RE_FM_CURRENT_ROW.match(stripped_clean)
        if m:
            context = m.group(1)
            enabled = m.group(2).strip().lower()
            if enabled == "true":
                result.append(f"{indent}/* current row highlight: apply :hover/:focus styles in {context} */")
            continue

        # Any other -fm- property not handled above → emit as comment
        if stripped_clean.startswith('-fm-') and ':' in stripped_clean:
            result.append(f"{indent}/* {stripped_clean} */")
            continue

        # Standard CSS property — pass through (without FM-specific comment)
        result.append(clean)

    # Prepend icon helper comment
    header = (
        "/* theme-web.css — Web-compatible translation of FileMaker theme CSS */\n"
        "/* FM-specific properties have been translated to web equivalents. */\n"
        "/* Icon glyphs use CSS mask-image with inline SVG data URIs. */\n"
        "/* -fm-icon-color maps to CSS color, which drives currentColor in the SVG mask. */\n"
    )
    return header + "\n".join(result)


# ---------------------------------------------------------------------------
# CSS shorthand consolidation
# ---------------------------------------------------------------------------

# Groups of directional properties that can be collapsed into shorthand.
# Each entry: (shorthand_name, [top, right, bottom, left])
_SHORTHAND_GROUPS = [
    ("border-color",  ["border-top-color", "border-right-color", "border-bottom-color", "border-left-color"]),
    ("border-style",  ["border-top-style", "border-right-style", "border-bottom-style", "border-left-style"]),
    ("border-width",  ["border-top-width", "border-right-width", "border-bottom-width", "border-left-width"]),
    ("border-radius", ["border-top-right-radius", "border-bottom-right-radius", "border-bottom-left-radius", "border-top-left-radius"]),
    ("margin",        ["margin-top", "margin-right", "margin-bottom", "margin-left"]),
    ("padding",       ["padding-top", "padding-right", "padding-bottom", "padding-left"]),
]


def _parse_declarations(lines):
    """Parse CSS declaration lines into a list of (property, value, comment, raw_line) tuples.

    Lines that are not declarations (selectors, braces, blank) are returned with
    property=None so they can be emitted verbatim.
    """
    decls = []
    for line in lines:
        stripped = line.strip()
        # Match: property: value;  /* optional comment */
        m = re.match(r'^([\w-]+)\s*:\s*(.+?)\s*;\s*(\/\*.*\*\/)?\s*$', stripped)
        if m:
            decls.append((m.group(1), m.group(2), m.group(3) or "", line))
        else:
            decls.append((None, None, None, line))
    return decls


def _shorthand_value(values):
    """Produce the shortest shorthand value for a list of [top, right, bottom, left].

    CSS shorthand rules:
      - All same:        value
      - Top/bottom same, left/right same: top_bottom left_right
      - Left/right same: top left_right bottom
      - All different:   top right bottom left
    """
    top, right, bottom, left = values
    if top == right == bottom == left:
        return top
    if top == bottom and right == left:
        return f"{top} {right}"
    if right == left:
        return f"{top} {right} {bottom}"
    return f"{top} {right} {bottom} {left}"


def consolidate_css(css_text):
    """Collapse directional CSS properties into shorthand equivalents.

    Processes each rule block independently. Only collapses when ALL four
    directional properties are present and adjacent (no other declarations
    between them).
    """
    result_lines = []
    block_lines = []
    in_block = False

    for line in css_text.split("\n"):
        stripped = line.strip()

        if stripped == "{":
            in_block = True
            block_lines = []
            result_lines.append(line)
            continue

        if stripped.startswith("}"):
            if in_block:
                result_lines.extend(_consolidate_block(block_lines))
            in_block = False
            result_lines.append(line)
            continue

        if in_block:
            block_lines.append(line)
        else:
            result_lines.append(line)

    return "\n".join(result_lines)


def _consolidate_block(block_lines):
    """Process a single CSS rule block, collapsing directional properties."""
    decls = _parse_declarations(block_lines)
    output = []
    skip_indices = set()

    for shorthand_name, prop_names in _SHORTHAND_GROUPS:
        # Find indices of all four directional properties
        indices = {}
        for i, (prop, val, comment, raw) in enumerate(decls):
            if prop in prop_names and i not in skip_indices:
                indices[prop] = i

        # Only collapse if all four are present
        if len(indices) == len(prop_names):
            # Check they're adjacent (no non-blank declarations between them)
            idx_list = sorted(indices.values())
            # Gather only declaration indices between first and last
            between = [j for j in range(idx_list[0], idx_list[-1] + 1)
                       if decls[j][0] is not None and j not in skip_indices]
            if set(between) != set(idx_list):
                continue  # Other declarations interleaved — skip

            # Extract values in top/right/bottom/left order
            values = [decls[indices[p]][1] for p in prop_names]

            # Collect any FM-specific comments
            has_fm_comment = any(decls[indices[p]][2] for p in prop_names)
            fm_suffix = "  /* FM-specific */" if has_fm_comment else ""

            # Determine indentation from first property
            first_raw = decls[indices[prop_names[0]]][3]
            indent = first_raw[:len(first_raw) - len(first_raw.lstrip())]

            # Build shorthand line
            short_val = _shorthand_value(values)
            shorthand_line = f"{indent}{shorthand_name}: {short_val};{fm_suffix}"

            # Mark the last three for skip; replace the first with the shorthand
            first_idx = idx_list[0]
            for p in prop_names:
                skip_indices.add(indices[p])
            skip_indices.discard(first_idx)  # Keep the first — it holds the shorthand
            decls[first_idx] = (shorthand_name, short_val, fm_suffix, shorthand_line)

    # Emit non-skipped lines
    for i, (prop, val, comment, raw) in enumerate(decls):
        if i not in skip_indices:
            output.append(raw)

    return output


def scan_layout_classes(ctx):
    """Scan every layout's raw XML for LocalCSS references.

    Returns a dict of {class_name: {"displayName": ..., "css": ..., "layouts": [...]}}.
    Layouts are named "<name> - ID <id>", as the exploded files were.
    """
    classes = {}

    layouts = sorted(ctx.layouts.values(), key=lambda lay: (lay.name.casefold(), int(lay.fmp_id or 0)))
    for lay in layouts:
        try:
            root = ET.fromstring(ctx.raw_xml(lay.doc_id))
        except (ET.ParseError, sc.SolutionContextError):
            continue

        layout_name = f"{lay.name} - ID {lay.fmp_id}"

        for local_css in root.iter("LocalCSS"):
            name = local_css.get("name", "")
            display_name = local_css.get("displayName", "")
            css_content = (local_css.text or "").strip()

            # Skip unnamed inline overrides (empty name = custom one-off style)
            if not name:
                continue

            if name not in classes:
                classes[name] = {
                    "displayName": display_name,
                    "css": css_content if css_content else None,
                    "layouts": []
                }

            # Track which layouts use this class
            if layout_name not in classes[name]["layouts"]:
                classes[name]["layouts"].append(layout_name)

            # If we haven't captured CSS yet and this one has it, use it
            if classes[name]["css"] is None and css_content:
                classes[name]["css"] = css_content

    return classes


def build_theme_manifest_from_xml(theme_root, css_text):
    """Build the structured theme-manifest.json data from the theme XML alone."""
    manifest = {
        "theme": {
            "name": theme_root.get("Display", ""),
            "id": int(theme_root.get("id", 0)),
            "internalName": theme_root.get("name", ""),
        }
    }

    # Add baseName if present (custom themes derived from a base)
    base_name = theme_root.get("baseName")
    if base_name:
        manifest["theme"]["baseTheme"] = base_name

    metadata = theme_root.find("Metadata")
    if metadata is not None:
        manifest["colorPalette"] = parse_color_palette(metadata)
        manifest["layoutBuilder"] = parse_layout_builder(metadata)
        manifest["charting"] = parse_charting(metadata)

        raw_styles = parse_named_styles(metadata)
        named_styles = []
        for style in raw_styles:
            style_css = extract_css_for_style(css_text, style["name"])
            named_styles.append({
                "name": style["name"],
                "displayName": style["displayName"],
                "css": style_css if style_css else ""
            })
        manifest["namedStyles"] = named_styles
    else:
        manifest["colorPalette"] = {}
        manifest["layoutBuilder"] = {}
        manifest["charting"] = {}
        manifest["namedStyles"] = []

    manifest["objectTypes"] = extract_object_types(css_text)

    return manifest


def build_theme_manifest(theme_root, css_text, theme=None):
    """Build the structured theme-manifest.json data.

    With ``theme`` (the fm-saxml model's theme entity), the metadata comes from the model;
    the CSS of each named style is always cut from the theme's CSS.
    """
    manifest = build_theme_manifest_from_xml(theme_root, css_text)
    if theme is None:
        return manifest

    manifest["theme"]["name"] = theme.display_name or ""
    manifest["theme"]["id"] = int(theme.fmp_id or 0)
    manifest["theme"]["internalName"] = theme.name or ""
    if theme.base_name:
        manifest["theme"]["baseTheme"] = theme.base_name
    if theme.color_palette is not None:
        manifest["colorPalette"] = dict(theme.color_palette)
    if theme.layout_builder is not None:
        manifest["layoutBuilder"] = {k: _int_or_text(v) for k, v in theme.layout_builder.items()}
    if theme.charting is not None:
        manifest["charting"] = dict(theme.charting)
    if theme.named_styles is not None:
        manifest["namedStyles"] = [{"name": name, "displayName": display or "",
                                    "css": extract_css_for_style(css_text, name) or ""}
                                   for name, display in theme.named_styles.items()]
    return manifest


def _int_or_text(value):
    try:
        return int(value)
    except (ValueError, TypeError):
        return value or ""


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Extract FileMaker theme data from a solution's fm-saxml docs into CSS and a JSON manifest."
    )
    sc.add_arguments(parser)
    parser.add_argument("--theme", help="theme display name, internal name or docId "
                                        "(default: the file's default theme)")
    parser.add_argument("-o", "--output", help="output folder (default: agent/context/{solution}/)")
    parser.add_argument(
        "--list",
        action="store_true",
        help="List the solution's themes"
    )
    args = parser.parse_args(argv)

    try:
        ctx = sc.from_args(args)
        if args.list:
            list_themes(ctx)
            return 0
        theme = pick_theme(ctx, args.theme)
        if theme is None:
            print(f"Error: {ctx.name} has no themes ({ctx.path})", file=sys.stderr)
            return 1
        theme_root = ET.fromstring(ctx.raw_xml(theme.doc_id))
        layout_classes = scan_layout_classes(ctx)
    except sc.SolutionContextError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    solution = ctx.name
    theme_name = theme.display_name or "Unknown"
    theme_id = theme.fmp_id or "?"
    print(f"Using theme: {theme_name} (ID {theme_id})")

    # Extract CSS
    css_text = extract_css(theme_root)
    if not css_text:
        print("Warning: No CSS content found in theme.")

    # Add FM-specific property comments, then consolidate shorthand
    css_output = add_fm_property_comments(css_text)
    css_output = consolidate_css(css_output)

    # Produce web-compatible translation (consolidate first for cleaner input)
    css_web = translate_fm_to_web(css_output)

    # Count CSS rules (selectors followed by {)
    rule_count = len(re.findall(r'\{', css_text))

    # Build manifest
    manifest = build_theme_manifest(theme_root, css_text, theme)

    # Build theme-classes.json
    theme_classes = {}
    for class_name, info in sorted(layout_classes.items()):
        entry = {
            "displayName": info["displayName"],
            "layouts": sorted(info["layouts"])
        }
        if info["css"]:
            entry["css"] = info["css"]
        else:
            entry["source"] = "inherits from theme"
        theme_classes[class_name] = entry

    # Create output directory
    output_dir = Path(args.output) if args.output else get_agent_root() / "context" / solution
    output_dir.mkdir(parents=True, exist_ok=True)

    # Write theme.css (faithful extraction with FM-specific annotations)
    css_path = output_dir / "theme.css"
    with open(css_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(css_output)

    # Write theme-web.css (web-compatible translation for preview rendering)
    css_web_path = output_dir / "theme-web.css"
    with open(css_web_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(css_web)

    # Write theme-manifest.json
    manifest_path = output_dir / "theme-manifest.json"
    with open(manifest_path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
        f.write("\n")

    # Write theme-classes.json
    classes_path = output_dir / "theme-classes.json"
    with open(classes_path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(theme_classes, f, indent=2, ensure_ascii=False)
        f.write("\n")

    # Summary
    print(f"\nExtracted theme data for '{solution}':")
    print(f"  Theme:         {theme_name} (ID {theme_id})")
    print(f"  CSS rules:     {rule_count}")
    print(f"  Named styles:  {len(manifest.get('namedStyles', []))}")
    print(f"  Color swatches: {len(manifest.get('colorPalette', {}))}")
    print(f"  Layout classes: {len(theme_classes)}")
    print("\nOutput files:")
    print(f"  {css_path}")
    print(f"  {css_web_path}  (web-compatible)")
    print(f"  {manifest_path}")
    print(f"  {classes_path}")
    return 0


if __name__ == "__main__":
    # Windows pipes and redirects default to the ANSI code page: accented names would garble
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
    sys.exit(main())
