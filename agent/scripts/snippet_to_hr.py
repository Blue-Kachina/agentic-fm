#!/usr/bin/env python3
"""
snippet_to_hr.py — Convert fmxmlsnippet XML to human-readable (HR) script text.

Usage:
    python3 agent/scripts/snippet_to_hr.py <path-to-snippet.xml>
    python3 agent/scripts/snippet_to_hr.py <snippet.xml> --output <output.txt>
    python3 agent/scripts/snippet_to_hr.py <snippet.xml> --raw

Each <Step> element produces exactly one output line, matching what a developer
sees in FileMaker Script Workspace.

By default, output includes line numbers (tab-separated). Use --raw for plain
text without line numbers (suitable for diff payloads).

Indentation follows Script Workspace rules:
    If, Loop         → render at current level, then indent +1
    Else, Else If    → indent -1, render, then indent +1
    End If, End Loop → indent -1, render

Disabled steps are prefixed with '// '.

Step rendering is driven by agent/catalogs/step-catalog-en.json via the shared
catalog_grammar engine (a faithful port of the reference grammar interpreter).
Only control-flow steps (If/Loop/…), Set Variable, and '# (comment)' remain
hand-coded — they carry block indentation or are structurally unique; every
other step is rendered generically from the catalog grammar.

This is the server-side Python equivalent of webviewer/src/converter/xml-to-hr.ts.
"""

import os
import sys
import xml.etree.ElementTree as ET

import catalog_grammar
from calc_literal import hr_sep

INDENT = "    "  # 4 spaces per level


# ---------------------------------------------------------------------------
# Catalog loading
# ---------------------------------------------------------------------------

def _find_catalog():
    """Locate step-catalog-en.json relative to the repo root."""
    here = os.path.dirname(os.path.abspath(__file__))
    for _ in range(6):
        candidate = os.path.join(here, '..', 'catalogs', 'step-catalog-en.json')
        candidate = os.path.normpath(candidate)
        if os.path.isfile(candidate):
            return candidate
        here = os.path.dirname(here)
    return None


def _load_grammar_entries():
    """Load the catalog into catalog_grammar CatalogEntry objects, keyed by name.

    These drive the generic catalog-driven renderer (catalog_grammar.render_step_hr),
    which is the single source of step HR structure for every non-control step.
    """
    path = _find_catalog()
    if path is None:
        return {}
    return {e.name: e for e in catalog_grammar.load_catalog(path)}


GRAMMAR_ENTRIES = _load_grammar_entries()


# ---------------------------------------------------------------------------
# XML helpers
# ---------------------------------------------------------------------------

def _close(line, last_value):
    """Close a step's HR line: ' ]', or a line break and ']' when its last value ends
    in a ``//`` calculation comment the one-line reading would cut
    (``calc_literal.hr_sep``)."""
    return line + hr_sep(last_value, ' ]')


def _calc(el, selector='Calculation'):
    """Extract text from a <Calculation> child (handles CDATA transparently)."""
    if el is None:
        return ''
    node = el.find(selector)
    if node is not None and node.text:
        return node.text
    return ''


# ---------------------------------------------------------------------------
# Step renderers — hand-coded for structurally unique steps
# ---------------------------------------------------------------------------

# A comment whose text holds a return is drawn by FileMaker on several Script
# Workspace lines, each continuation aligned under the text after ``# ``. HR writes
# it the same way: the break becomes a new line indented past the comment's own
# indentation, two spaces for a CR (the return FileMaker itself stores, and the
# form an author writes) and one tab for an LF (FileMaker keeps an LF a pasted
# ``&#10;`` put there). convert() indents those lines with the step.
COMMENT_CR_PAD, COMMENT_LF_PAD = '  ', '\t'


def encode_hr_comment_text(text):
    """*text* with each line break written as an HR continuation (column 0)."""
    pads = {'\r': '\n' + COMMENT_CR_PAD, '\n': '\n' + COMMENT_LF_PAD}
    return ''.join(pads.get(c, c) for c in text)


def _render_comment(step):
    """# (comment) — id 89"""
    text_el = step.find('Text')
    text = text_el.text if text_el is not None and text_el.text else ''
    if text:
        return f'# {encode_hr_comment_text(text)}', (False, False)
    return '', (False, False)  # blank line


def _render_if(step):
    """If — id 68"""
    calc = _calc(step)
    return (_close(f'If [ {calc}', calc) if calc else 'If'), (False, True)


def _render_else_if(step):
    """Else If — id 125"""
    calc = _calc(step)
    return (_close(f'Else If [ {calc}', calc) if calc else 'Else If'), (True, True)


def _render_else(step):
    """Else — id 69"""
    return 'Else', (True, True)


def _render_end_if(step):
    """End If — id 70"""
    return 'End If', (True, False)


def _render_loop(step):
    """Loop — id 71.

    Two catalog options: Collapsed -> <Restore state="True|False"/> (default
    Off/False) and Flush -> <FlushType value="Always|Min|Defer"/> (default
    Always). FileMaker WRITES the Minimum flush as the token "Min" but DISPLAYS
    "Minimum" in the Script Workspace, so render the display label. Emit a clause
    only when non-default, so a plain Loop stays bare "Loop".
    """
    clauses = []
    restore = step.find('Restore')
    if restore is not None and (restore.get('state') or '').lower() == 'true':
        clauses.append('Collapsed: On')
    flush_el = step.find('FlushType')
    flush = (flush_el.get('value') if flush_el is not None else '') or ''
    if flush and flush.lower() != 'always':
        label = 'Minimum' if flush.lower() == 'min' else flush
        clauses.append(f'Flush: {label}')
    hr = 'Loop [ ' + ' ; '.join(clauses) + ' ]' if clauses else 'Loop'
    return hr, (False, True)


def _render_exit_loop_if(step):
    """Exit Loop If — id 72"""
    calc = _calc(step)
    return _close(f'Exit Loop If [ {calc}', calc), (False, False)


def _render_end_loop(step):
    """End Loop — id 73"""
    return 'End Loop', (True, False)


def _render_exit_script(step):
    """Exit Script — id 103"""
    calc = _calc(step)
    if calc:
        return _close(f'Exit Script [ Text Result: {calc}', calc), (False, False)
    return 'Exit Script', (False, False)


def _render_set_variable(step):
    """Set Variable — id 141"""
    name = ''
    name_el = step.find('Name')
    if name_el is not None and name_el.text:
        name = name_el.text

    value = _calc(step, 'Value/Calculation')
    rep = _calc(step, 'Repetition/Calculation')

    rep_suffix = ''
    if rep and rep.strip() != '1':
        rep_suffix = f'[{rep.strip()}]'

    return _close(f'Set Variable [ {name}{rep_suffix} ; Value: {value}', value), (False, False)



# Hand-coded renderer dispatch — keyed by step name (from XML name attribute)
RENDERERS = {
    # The sanctioned hand-coded exceptions. Control-flow steps carry block
    # indentation (the (close_before, open_after) tuple) the engine does not model;
    # '# (comment)' and 'Set Variable' are structurally unique. Every other step is
    # rendered from the catalog via catalog_grammar.render_step_hr.
    '# (comment)': _render_comment,
    'If': _render_if,
    'Else If': _render_else_if,
    'Else': _render_else,
    'End If': _render_end_if,
    'Loop': _render_loop,
    'Exit Loop If': _render_exit_loop_if,
    'End Loop': _render_end_loop,
    'Exit Script': _render_exit_script,
    'Set Variable': _render_set_variable,
}


# ---------------------------------------------------------------------------
# Main renderer
# ---------------------------------------------------------------------------

def render_step(step):
    """
    Render a single <Step> element to HR text.

    Control-flow + structurally-unique steps (RENDERERS) are hand-coded; every
    other step is rendered from the catalog via catalog_grammar.render_step_hr —
    a faithful port of the reference grammar interpreter that reads the full
    catalog grammar (discriminators, hrSlot ordering, hrEnumValues + enumStyle,
    attrGroup/bitmask/repeat/fieldList, visibleWhen/hrLabelWhen/hrHidden,
    flagBoolean normalization). Non-control steps carry no block indentation.

    Returns:
        (line_text, (close_before, open_after))
    """
    step_name = step.get('name', '')

    renderer = RENDERERS.get(step_name)
    if renderer:
        return renderer(step)

    entry = GRAMMAR_ENTRIES.get(step_name)
    if entry is None:
        return step_name, (False, False)
    return catalog_grammar.render_step_hr(entry, step), (False, False)


def snippet_to_hr(xml_string):
    """
    Convert an fmxmlsnippet XML string to human-readable script text.

    Returns a list of HR lines (without line numbers).
    """
    root = ET.fromstring(xml_string)

    steps = root.findall('Step')
    lines = []
    indent = 0

    for step in steps:
        enabled = step.get('enable', 'True') == 'True'

        text, (close_before, open_after) = render_step(step)

        if close_before:
            indent = max(0, indent - 1)

        # Add disabled prefix
        if not enabled:
            text = f'// {text}'

        prefix = INDENT * indent
        if step.get('name') == '# (comment)' and prefix:
            text = text.replace('\n', '\n' + prefix)  # continuation lines sit under it
        lines.append(prefix + text)

        if open_after:
            indent += 1

    return lines


def convert_file(xml_path, raw=False):
    """Parse an fmxmlsnippet file and return HR text."""
    with open(xml_path, encoding='utf-8') as f:
        xml_string = f.read()

    lines = snippet_to_hr(xml_string)

    if raw:
        return '\n'.join(lines)

    numbered = []
    for i, line in enumerate(lines, 1):
        numbered.append(f'{i}\t{line}')
    return '\n'.join(numbered)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    # Windows pipes and redirects default to the ANSI code page: accented names would garble
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8', errors='backslashreplace')
    import argparse

    parser = argparse.ArgumentParser(
        description='Convert fmxmlsnippet XML to human-readable script text.'
    )
    parser.add_argument('input', help='Path to fmxmlsnippet XML file')
    parser.add_argument('--output', '-o', help='Write output to file instead of stdout')
    parser.add_argument('--raw', action='store_true',
                        help='Plain text without line numbers (for diff payloads)')
    args = parser.parse_args()

    result = convert_file(args.input, raw=args.raw)

    if args.output:
        with open(args.output, 'w', encoding='utf-8') as f:
            f.write(result)
            f.write('\n')
        print(f'Written to {args.output}', file=sys.stderr)
    else:
        print(result)
