#!/usr/bin/env python3
"""
Cross-reference tracer for FileMaker solutions.

Builds a cross-reference index from the solution's fm-saxml docs (the typed
references in references.json, read through solution_context.py) and supports
targeted queries and dead-object detection.

Usage:
  python3 trace.py build  -s "Solution Name" [--docs-root <folder>] [--file <name>] [--context <name>]
  python3 trace.py query  -s "Solution Name" -t field -n "Clients::Name"
  python3 trace.py query  -s "Solution Name" -t script -n "Print Invoice"
  python3 trace.py dead   -s "Solution Name" -t fields
  python3 trace.py dead   -s "Solution Name" -t scripts
  python3 trace.py dead   -s "Solution Name" -t custom_functions

Output:
  build  → writes agent/context/{solution}/xref.index
  query  → prints references to/from the named object
  dead   → prints unreferenced objects with confidence levels
"""

import argparse
import json
import os
import re
import sys
from collections import namedtuple
from pathlib import Path

import solution_context


# ---------------------------------------------------------------------------
# Project root resolution
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent  # agent/scripts/ → project root

CONTEXT_DIR = PROJECT_ROOT / "agent" / "context"
XML_PARSED_DIR = PROJECT_ROOT / "agent" / "xml_parsed"
CONFIG_DIR = PROJECT_ROOT / "agent" / "config"


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

XRef = namedtuple("XRef", [
    "source_type",      # field_calc, field_auto, script, layout, custom_func, relationship, value_list
    "source_name",      # "Invoices::Client Name", "Print Invoice (ID 158)"
    "source_location",  # "calc:Clients Primary::Name", "line 14: Set Field"
    "ref_type",         # field, script, layout, value_list, custom_func, table_occurrence
    "ref_name",         # canonical: "Clients::Name" (base table, not TO)
    "ref_context",      # "via TO \"Clients Primary\"", "same table", ""
])


# ---------------------------------------------------------------------------
# System fields excluded from dead-object scans
# ---------------------------------------------------------------------------

SYSTEM_FIELDS = {
    "PrimaryKey", "CreationTimestamp", "CreatedBy",
    "ModificationTimestamp", "ModifiedBy",
}


# ---------------------------------------------------------------------------
# Index loaders (query and dead; build reads the fm-saxml model instead)
# ---------------------------------------------------------------------------

def _parse_index(path, columns):
    """Parse a pipe-delimited index file into a list of dicts."""
    rows = []
    if not path.exists():
        return rows
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            parts = line.split("|")
            row = {}
            for i, col in enumerate(columns):
                row[col] = parts[i] if i < len(parts) else ""
            rows.append(row)
    return rows


def load_fields_index(solution_dir):
    return _parse_index(
        solution_dir / "fields.index",
        ["table", "table_id", "field", "field_id", "datatype",
         "fieldtype", "auto_enter", "flags"],
    )


def load_table_occurrences_index(solution_dir):
    return _parse_index(
        solution_dir / "table_occurrences.index",
        ["to_name", "to_id", "base_table", "base_table_id"],
    )


def load_scripts_index(solution_dir):
    return _parse_index(
        solution_dir / "scripts.index",
        ["name", "id", "folder"],
    )


def load_layouts_index(solution_dir):
    return _parse_index(
        solution_dir / "layouts.index",
        ["name", "id", "base_to", "base_to_id", "folder"],
    )


def load_value_lists_index(solution_dir):
    return _parse_index(
        solution_dir / "value_lists.index",
        ["name", "id", "source_type", "values"],
    )


def build_to_map(to_index):
    """Build {TOName: BaseTableName} mapping."""
    return {row["to_name"]: row["base_table"] for row in to_index}


def build_cf_names(solution_name):
    """Get list of custom function names and IDs from directory listing."""
    cf_dir = XML_PARSED_DIR / "custom_functions_sanitized" / solution_name
    cfs = []
    if not cf_dir.exists():
        return cfs
    # rglob (not iterdir) so functions nested inside custom-function group
    # folders (e.g. "Object Functions - ID 53/") are found, not missed.
    for f in cf_dir.rglob("*.txt"):
        # Parse "FuncName - ID NNN.txt"
        m = re.match(r'^(.+?)\s*-\s*ID\s+(\d+)\.txt$', f.name)
        if not m:
            continue
        # Skip empty / whitespace-only stubs — these are custom-function group
        # or folder headers (and separators like "--"), not real functions.
        # Without this, their names get counted as CFs and then flagged as
        # dead by the unused-CF scan.
        try:
            if not f.read_text(encoding="utf-8").strip():
                continue
        except OSError:
            continue
        cfs.append({"name": m.group(1), "id": m.group(2), "path": f})
    return cfs


# ---------------------------------------------------------------------------
# Build: fm-saxml references -> XRef rows
# ---------------------------------------------------------------------------
#
# fm-saxml's references.json already resolves every reference (script steps,
# calculations, layout objects, triggers, relationships, value lists, custom
# menus) to a target docId. Build keeps the ones whose target is a kind trace
# reports and names them the way xref.index always has:
#
#   source entity           SourceType     SourceName              SourceLocation
#   scriptStep              script         "Script (ID n)"         "line N: <step>" (N = Script Workspace line)
#   field (calculation)     field_calc     "Table::Field"          "calc" | "validation" | "summary" ...
#   field (auto-enter)      field_auto     "Table::Field"          "auto-enter" | "lookup"
#   layout, layoutObject    layout         "Layout (ID n)"         "field placement" | "button script" | "trigger: <event>" ...
#   relationship            relationship   "LeftTO→RightTO"        "join field" | "sort field"
#   customFunction          custom_func    "Function (ID n)"       "calc body"
#   valueList               value_list     "List (ID n)"           "primary field" | "secondary field" | "table occurrence"
#   fileOptions             file           "File"                  "trigger: <event>" | "default layout"
#   customMenu              custom_menu    "Menu (ID n)"           "menu item" | "install condition"
#
# RefName is the target's name; a field is "BaseTable::Field". RefContext says how
# a field was reached ('via TO "X"', "same table", "left side"/"right side") and
# flags references fm-saxml marks as by name, unresolved or in another file.
# Not kept: structure (contains, based on, joins to), privileges and accounts.

REF_TYPE_FOR_TARGET = {
    "field": "field",
    "script": "script",
    "layout": "layout",
    "valueList": "value_list",
    "customFunction": "custom_func",
    "to": "table_occurrence",
    "tableOccurrence": "table_occurrence",
}
STRUCTURAL = {"contains", "basedOnTableOccurrence", "basedOnBaseTable", "joinsTo",
              "assignedPrivilegeSet", "grantedTo"}
FIELD_AUTO_ROLES = {"autoEnter", "lookupSource"}
LOCATION_FOR_ROLE = {
    "calculation": "calc", "autoEnter": "auto-enter", "lookupSource": "lookup",
    "validation": "validation", "validationMessage": "validation message",
    "display": "field placement", "button": "button script", "control": "value list control",
    "hideCondition": "hide condition", "tooltip": "tooltip", "placeholder": "placeholder",
    "conditionalFormatting": "conditional formatting", "buttonParameter": "button parameter",
    "buttonAction": "button action", "scriptTriggerParameter": "trigger parameter",
    "portalSort": "portal sort", "portalFilter": "portal filter", "webViewer": "web viewer",
    "tabLabel": "tab label", "popoverTitle": "popover title", "relatedFrom": "related values from",
    "source": "primary field", "second": "secondary field",
    "sourceTableOccurrence": "table occurrence", "external": "external values",
    "menuItem": "menu item", "installCondition": "install condition",
    "defaultLayout": "default layout",
}
# Roles a layout or layout object gives a script trigger are event names (OnRecordLoad...)
TRIGGER_RELATIONSHIP = "triggersScript"


def _named(entity, fallback):
    return getattr(entity, "name", None) or fallback


def _with_id(entity, fallback):
    name = _named(entity, fallback)
    fmp_id = getattr(entity, "fmp_id", None)
    return f"{name} (ID {fmp_id})" if fmp_id else name


def _doc_name(doc_id):
    """The name part of a docId: "script:Print" -> "Print", "field:T::F" -> "T::F"."""
    return doc_id.split(":", 1)[1] if ":" in doc_id else doc_id


def _field_context(ref, source_type, to_name):
    """How a field was reached: via another TO, same table, or nothing to add.

    SaXML stores every calculation field reference with its TO, even when the formula
    names the field unqualified, so "same table" means: a field calculation reaching a
    field of its own table through the TO named after that table.
    """
    base = _doc_name(ref.target_doc_id).split("::", 1)[0]
    if to_name and to_name != base:
        return f'via TO "{to_name}"'
    if source_type in ("field_calc", "field_auto") and _doc_name(ref.source_doc_id).split("::", 1)[0] == base:
        return "same table"
    return ""


def _to_name_from_raw(raw_text):
    """'Invoices Lines::Amount' -> 'Invoices Lines' (quotes and $ variables ignored)."""
    if not raw_text or "::" not in raw_text:
        return ""
    return raw_text.strip().strip('"').split("::", 1)[0].strip()


class _XRefBuilder:
    def __init__(self, ctx):
        self.ctx = ctx
        self.scripts = ctx.collection("script")
        self.steps = ctx.collection("scriptStep")
        self.layouts = ctx.layouts
        self.objects = ctx.collection("layoutObject")
        self.fields = ctx.collection("field")
        self.tos = ctx.collection("to")
        self.relationships = ctx.collection("relationship")
        self.cfs = ctx.collection("customFunction")
        self.value_lists = ctx.collection("valueList")
        self.menus = ctx.collection("customMenu")
        # (layout docId, target, role) a layout object already gives: fm-saxml rolls an
        # object's references up to its layout too, and a placement must count once.
        self.object_refs = set()
        for r in ctx.references:
            obj = self.objects.get(r.source_doc_id) if r.source_entity_type == "layoutObject" else None
            if obj is not None:
                self.object_refs.add((obj.layout_doc_id, r.target_doc_id, r.role))

    def source(self, ref):
        """(SourceType, SourceName, SourceLocation, TO the field was reached through), or None to skip."""
        kind, sid, role = ref.source_entity_type, ref.source_doc_id, ref.role
        location = (f"trigger: {role}" if ref.relationship_type == TRIGGER_RELATIONSHIP and role
                    and role not in LOCATION_FOR_ROLE else LOCATION_FOR_ROLE.get(role or "", role or ""))

        if kind == "scriptStep":
            step = self.steps.get(sid)
            script = self.scripts.get(step.script_doc_id) if step else None
            if step is None:
                return "script", _doc_name(sid), "", ""
            return ("script", _with_id(script, _doc_name(step.script_doc_id)),
                    f"line {step.index + 1}: {step.name}", _to_name_from_raw(ref.raw_text))

        if kind == "field":
            if ref.relationship_type == "summarizes":
                location = "summary"
            source_type = "field_auto" if role in FIELD_AUTO_ROLES else "field_calc"
            return source_type, _doc_name(sid), location or "calc", _to_name_from_raw(ref.raw_text)

        if kind in ("layout", "layoutObject"):
            if kind == "layoutObject":
                obj = self.objects.get(sid)
                layout_id = obj.layout_doc_id if obj else ""
                to_doc = getattr(obj, "table_occurrence_doc_id", None) if obj else None
                to_name = _doc_name(to_doc) if to_doc else _to_name_from_raw(ref.raw_text)
            else:
                layout_id = sid
                if (sid, ref.target_doc_id, role) in self.object_refs:
                    return None   # rolled up from one of its objects
                to_name = _to_name_from_raw(ref.raw_text)
            layout = self.layouts.get(layout_id)
            return "layout", _with_id(layout, _doc_name(layout_id)), location, to_name

        if kind == "relationship":
            rel = self.relationships.get(sid)
            left = _doc_name(rel.left_table_occurrence_doc_id) if rel else ""
            right = _doc_name(rel.right_table_occurrence_doc_id) if rel else ""
            sort = (role or "").endswith("Sort")
            return ("relationship", f"{left}→{right}", "sort field" if sort else "join field",
                    left if (role or "").startswith("left") else right)

        if kind == "customFunction":
            return "custom_func", _with_id(self.cfs.get(sid), _doc_name(sid)), "calc body", \
                _to_name_from_raw(ref.raw_text)

        if kind == "valueList":
            return "value_list", _with_id(self.value_lists.get(sid), _doc_name(sid)), location, \
                _to_name_from_raw(ref.raw_text)

        if kind == "fileOptions":
            return "file", "File", location, ""

        if kind == "customMenu":
            return "custom_menu", _with_id(self.menus.get(sid), _doc_name(sid)), location, \
                _to_name_from_raw(ref.raw_text)

        return None

    def xref(self, ref):
        ref_type = REF_TYPE_FOR_TARGET.get(ref.target_entity_type)
        if ref_type is None or ref.relationship_type in STRUCTURAL:
            return None
        src = self.source(ref)
        if src is None:
            return None
        source_type, source_name, location, to_name = src

        notes = []
        ext = ref.external_target
        if ext is not None:
            name = ext.name or _doc_name(ext.target_doc_id)
            if ref_type == "field" and ext.base_table and "::" not in name:
                name = f"{ext.base_table}::{name}"
            notes.append(f'external file "{ext.data_source}"')
        else:
            target = self.ctx.get(ref.target_doc_id)
            if ref_type == "field":
                name = getattr(target, "qualified_name", None) or _doc_name(ref.target_doc_id)
            else:
                name = _named(target, _doc_name(ref.target_doc_id))
            if ref_type == "field":
                context = _field_context(ref, source_type, to_name)
                if source_type == "relationship":
                    context = "left side" if (ref.role or "").startswith("left") else "right side"
                if context:
                    notes.append(context)
            if ref.confidence == "unresolved":
                notes.append("unresolved")
        if ref.via == "byName":
            notes.append("by name")
        return XRef(source_type, source_name, location, ref_type, name, ", ".join(notes))


# Build progress lines: (label, the SourceTypes it counts), in the order they print
BUILD_GROUPS = [
    ("field calculations", {"field_calc", "field_auto"}),
    ("relationships", {"relationship"}),
    ("scripts", {"script"}),
    ("layouts", {"layout"}),
    ("file-level triggers", {"file"}),
    ("custom functions", {"custom_func"}),
    ("value lists", {"value_list"}),
    ("custom menus", {"custom_menu"}),
]


def build_xrefs(ctx):
    """XRef rows for every reference in the solution, in build-group order."""
    builder = _XRefBuilder(ctx)
    rows = [x for x in (builder.xref(r) for r in ctx.references) if x is not None]
    order = {t: i for i, (_, types) in enumerate(BUILD_GROUPS) for t in types}
    return sorted(rows, key=lambda x: order.get(x.source_type, len(order)))   # stable: keeps model order


def _context_name(args, ctx):
    """Folder under agent/context/ for this solution's xref.index (query and dead read it there).

    --context names it; else -s, or the FileMaker file name when -s is a docs folder or
    --file picks one file of a multi-file build.
    """
    if args.context:
        return args.context
    if args.file or not args.solution or os.sep in args.solution or "/" in args.solution:
        return ctx.name
    return args.solution


def _default_solution(docs_root):
    """The only solution under the docs root, for `build` without -s."""
    root = docs_root or os.environ.get(solution_context.DOCS_ROOT_ENV)
    if root and Path(root).is_dir():
        found = [d.name for d in Path(root).iterdir() if d.is_dir()
                 and ((d / "docs.json").is_file() or (d / "entities.json").is_file())]
        if len(found) == 1:
            return found[0]
        if found:
            print("Multiple solutions found. Specify one with -s:", file=sys.stderr)
            for s in sorted(found):
                print(f"  {s}", file=sys.stderr)
            sys.exit(1)
    return None


def cmd_build(args):
    """Build xref.index for the solution from its fm-saxml docs."""
    if not args.solution:
        args.solution = _default_solution(args.docs_root)
        if not args.solution:
            print("ERROR: no solution given: pass -s <solution> (and --docs-root or "
                  f"${solution_context.DOCS_ROOT_ENV})", file=sys.stderr)
            sys.exit(1)
    try:
        ctx = solution_context.from_args(args)
        all_refs = build_xrefs(ctx)
    except solution_context.SolutionContextError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    solution_name = _context_name(args, ctx)
    solution_dir = CONTEXT_DIR / solution_name
    solution_dir.mkdir(parents=True, exist_ok=True)

    print(f"==> Building xref.index for: {solution_name}")
    print(f"  Docs: {ctx.path} (fm-saxml contract {ctx.schema_version})")
    print(f"  Fields: {len(ctx.collection('field'))}, TOs: {len(ctx.collection('to'))}, "
          f"Scripts: {len(ctx.scripts)}, CFs: {len(ctx.collection('customFunction'))}")
    for label, types in BUILD_GROUPS:
        print(f"  Parsing {label}...")
        print(f"    {sum(1 for x in all_refs if x.source_type in types)} references found")

    # Write xref.index
    xref_path = solution_dir / "xref.index"
    with open(xref_path, "w", encoding="utf-8", newline="\n") as f:
        f.write("# SourceType|SourceName|SourceLocation|RefType|RefName|RefContext\n")
        for ref in all_refs:
            # Escape pipes in fields
            row = [
                ref.source_type,
                _escape_pipe(ref.source_name),
                _escape_pipe(ref.source_location),
                ref.ref_type,
                _escape_pipe(ref.ref_name),
                _escape_pipe(ref.ref_context),
            ]
            f.write("|".join(row) + "\n")

    print(f"\n==> Done! {len(all_refs)} total references")
    print(f"  Output: {xref_path}")



def _escape_pipe(s):
    """Escape pipe characters in index values."""
    return s.replace("|", "\\|")


def _unescape_pipe(s):
    """Unescape pipe characters in index values."""
    return s.replace("\\|", "|")


# ---------------------------------------------------------------------------
# Query command
# ---------------------------------------------------------------------------

def load_xref(solution_dir):
    """Load xref.index into list of XRef tuples."""
    xref_path = solution_dir / "xref.index"
    if not xref_path.exists():
        print(f"ERROR: xref.index not found. Run 'build' first.", file=sys.stderr)
        sys.exit(1)

    refs = []
    with open(xref_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            # Split on unescaped pipes
            parts = re.split(r'(?<!\\)\|', line)
            if len(parts) < 6:
                continue
            refs.append(XRef(
                _unescape_pipe(parts[0]),
                _unescape_pipe(parts[1]),
                _unescape_pipe(parts[2]),
                _unescape_pipe(parts[3]),
                _unescape_pipe(parts[4]),
                _unescape_pipe(parts[5]),
            ))
    return refs


def cmd_query(solution_name, ref_type, ref_name, direction):
    """Query references to/from an object."""
    solution_dir = CONTEXT_DIR / solution_name
    to_index = load_table_occurrences_index(solution_dir)
    to_map = build_to_map(to_index)

    # Resolve TO-qualified input to canonical form
    canonical_name = ref_name
    if ref_type == "field" and "::" in ref_name:
        parts = ref_name.split("::", 1)
        to_name, field_name = parts[0], parts[1]
        base = to_map.get(to_name, to_name)
        canonical_name = f"{base}::{field_name}"

    xrefs = load_xref(solution_dir)

    if direction == "inbound":
        # Who references this object?
        matches = [x for x in xrefs
                   if x.ref_type == ref_type and x.ref_name == canonical_name]
    else:
        # What does this object reference?
        matches = [x for x in xrefs
                   if x.source_name == ref_name or x.source_name == canonical_name]

    if not matches:
        print(f"No {'inbound' if direction == 'inbound' else 'outbound'} "
              f"references found for {ref_type}: {ref_name}")
        if canonical_name != ref_name:
            print(f"  (resolved to canonical: {canonical_name})")
        return

    # Group by source type
    label = "References to" if direction == "inbound" else "References from"
    print(f"=== {label} {ref_type}: {canonical_name} ===\n")

    groups = {}
    for ref in matches:
        key = ref.source_type if direction == "inbound" else ref.ref_type
        groups.setdefault(key, []).append(ref)

    # Display order
    type_labels = {
        "field_calc": "FIELD CALCULATIONS",
        "field_auto": "FIELD AUTO-ENTER",
        "script": "SCRIPTS",
        "layout": "LAYOUTS",
        "custom_func": "CUSTOM FUNCTIONS",
        "relationship": "RELATIONSHIPS",
        "value_list": "VALUE LISTS",
        "field": "FIELDS",
        "table_occurrence": "TABLE OCCURRENCES",
    }

    for group_key in type_labels:
        if group_key not in groups:
            continue
        items = groups[group_key]
        print(f"{type_labels[group_key]} ({len(items)}):")
        for ref in items:
            if direction == "inbound":
                ctx = f" \u2014 {ref.ref_context}" if ref.ref_context else ""
                print(f"  {ref.source_name}, {ref.source_location}{ctx}")
            else:
                ctx = f" \u2014 {ref.ref_context}" if ref.ref_context else ""
                print(f"  {ref.ref_type}: {ref.ref_name} ({ref.source_location}){ctx}")
        print()

    print(f"Summary: {len(matches)} references across {len(groups)} source type(s)")


# ---------------------------------------------------------------------------
# Dead object scan
# ---------------------------------------------------------------------------

def cmd_dead(solution_name, obj_type, verbose):
    """Find unreferenced objects."""
    solution_dir = CONTEXT_DIR / solution_name
    xrefs = load_xref(solution_dir)

    # Reliability guard: dead-object detection for scripts/fields/value_lists
    # leans on layout references (placements, button scripts, triggers). If the
    # xref has no layout-sourced refs, those edges are missing and the results
    # will over-report "dead" objects. Warn loudly rather than mislead a
    # human about to delete things.
    if obj_type in ("scripts", "fields", "value_lists"):
        has_layout_refs = any(ref.source_type == "layout" for ref in xrefs)
        if not has_layout_refs:
            print(
                "⚠️  WARNING: xref.index contains NO layout references — "
                f"'{obj_type}' dead results are UNRELIABLE.\n"
                "    Layout placements, button scripts and script triggers are "
                "missing, so trigger-only / layout-only objects will be "
                "falsely flagged as dead.\n"
                "    Regenerate layout summaries and rebuild before trusting "
                "this output:\n"
                f"      python3 agent/scripts/layout_to_summary.py --solution \"{solution_name}\"\n"
                f"      python3 agent/scripts/trace.py build -s \"{solution_name}\"\n",
                file=sys.stderr,
            )

    # Build set of all referenced objects by type
    referenced = set()
    for ref in xrefs:
        if ref.ref_type == _dead_ref_type(obj_type):
            referenced.add(ref.ref_name)

    # Build set of all objects of this type
    all_objects, on_layout, system_excluded, module_objects = _get_all_objects(
        solution_dir, solution_name, obj_type, xrefs,
    )

    # Compute dead = all - referenced
    unreferenced = all_objects - referenced

    # Classify confidence
    high = []
    medium = []
    low = []
    module = []

    for obj in sorted(unreferenced):
        if obj in module_objects:
            module.append(obj)
        elif obj in system_excluded:
            low.append(obj)
        elif obj in on_layout:
            medium.append(obj)
        else:
            high.append(obj)

    # Display
    print(f"=== Potentially unused {obj_type} ({solution_name}) ===\n")

    if high:
        print(f"HIGH CONFIDENCE — no references found anywhere ({len(high)}):")
        for obj in high:
            print(f"  {obj}")
        print()

    if medium:
        print(f"MEDIUM CONFIDENCE — on a layout but not in scripts/calcs ({len(medium)}):")
        for obj in medium:
            layouts = on_layout.get(obj, [])
            layout_str = ", ".join(layouts[:3])
            if len(layouts) > 3:
                layout_str += f" (+{len(layouts) - 3} more)"
            print(f"  {obj} \u2014 on layout: {layout_str}")
        print()

    if module:
        print(f"MODULE — installed tool objects, invoked externally — NOT dead ({len(module)}):")
        for obj in module:
            print(f"  {obj} — {module_objects[obj]}")
        print()

    if verbose and low:
        print(f"LOW CONFIDENCE — excluded by heuristics ({len(low)}):")
        for obj in low:
            print(f"  {obj}")
        print()

    total = len(all_objects)
    parts = [f"{len(high)} high", f"{len(medium)} medium"]
    if verbose:
        parts.append(f"{len(low)} low")
    tail = f" + {len(module)} module (live)" if module else ""
    print(f"Summary: {', '.join(parts)} unused{tail} "
          f"out of {total} total {obj_type}")


def _dead_ref_type(obj_type):
    """Map dead scan object type to xref ref_type."""
    mapping = {
        "fields": "field",
        "scripts": "script",
        "custom_functions": "custom_func",
        "layouts": "layout",
        "value_lists": "value_list",
    }
    return mapping.get(obj_type, obj_type)


def load_modules():
    """Load installed-module definitions.

    Modules are third-party tools (agentic-fm, InspectorPro, OttoFMS, …) whose
    objects are live but have no inbound references *inside* the solution — they
    are invoked externally (OData / fmurlscript / a companion app) or managed by
    the module itself. We surface them separately so they are never mistaken for
    the solution's own dead code.

    Definitions come from the shipped defaults (``modules.json.example``),
    overlaid by the developer's optional ``modules.json`` (merged by ``label``,
    so a user file does not need to re-declare the agentic-fm default).
    """
    by_label = {}
    for filename in ("modules.json.example", "modules.json"):
        path = CONFIG_DIR / filename
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        for entry in data.get("modules", []):
            label = entry.get("label")
            if label:
                by_label[label] = entry  # later file (user) overrides by label
    return list(by_label.values())


def match_module(name, folder, modules):
    """Return the matching module's label for (name, folder), else None.

    Matching is by object NAME first (folder-independent): an exact name match
    or a registered name prefix. ``folder_contains`` is a secondary hint for
    tools that live in a known folder. A match on ANY signal tags the object.
    """
    folder_l = (folder or "").lower()
    for mod in modules:
        if name in mod.get("name_exact", []):
            return mod["label"]
        for prefix in mod.get("name_prefixes", []):
            if prefix and name.startswith(prefix):
                return mod["label"]
        for token in mod.get("folder_contains", []):
            if token and token.lower() in folder_l:
                return mod["label"]
    return None


def _get_all_objects(solution_dir, solution_name, obj_type, xrefs):
    """Get all objects, plus layout-only, system-excluded and module objects."""
    system_excluded = set()
    on_layout = {}  # {obj_name: [layout_names]}
    module_objects = {}  # {obj_name: module_label}
    modules = load_modules()

    if obj_type == "fields":
        fields_index = load_fields_index(solution_dir)
        all_objects = set()
        for row in fields_index:
            canonical = f"{row['table']}::{row['field']}"
            all_objects.add(canonical)

            label = match_module(row["field"], "", modules) or match_module(row["table"], "", modules)
            if label:
                module_objects[canonical] = label

            # System exclusions
            if row["field"] in SYSTEM_FIELDS:
                system_excluded.add(canonical)
            elif row["field"].startswith("ForeignKey") or row["field"].startswith("FK"):
                system_excluded.add(canonical)
            elif "global" in row.get("flags", ""):
                system_excluded.add(canonical)
            elif row["fieldtype"] == "Summary":
                system_excluded.add(canonical)

        # Find fields that are only on layouts
        for ref in xrefs:
            if ref.ref_type == "field" and ref.source_type == "layout":
                on_layout.setdefault(ref.ref_name, []).append(
                    ref.source_name.split(" (ID")[0]
                )

    elif obj_type == "scripts":
        scripts_index = load_scripts_index(solution_dir)
        all_objects = set()
        for row in scripts_index:
            all_objects.add(row["name"])
            label = match_module(row["name"], row.get("folder", ""), modules)
            if label:
                module_objects[row["name"]] = label

        # Find scripts only on layouts
        for ref in xrefs:
            if ref.ref_type == "script" and ref.source_type == "layout":
                on_layout.setdefault(ref.ref_name, []).append(
                    ref.source_name.split(" (ID")[0]
                )

    elif obj_type == "custom_functions":
        cf_names = build_cf_names(solution_name)
        all_objects = set()
        for cf in cf_names:
            all_objects.add(cf["name"])
            label = match_module(cf["name"], "", modules)
            if label:
                module_objects[cf["name"]] = label

    elif obj_type == "layouts":
        layouts_index = load_layouts_index(solution_dir)
        all_objects = set()
        for row in layouts_index:
            all_objects.add(row["name"])
            label = match_module(row["name"], row.get("folder", ""), modules)
            if label:
                module_objects[row["name"]] = label

    elif obj_type == "value_lists":
        vl_index = load_value_lists_index(solution_dir)
        all_objects = set()
        for row in vl_index:
            all_objects.add(row["name"])
            label = match_module(row["name"], "", modules)
            if label:
                module_objects[row["name"]] = label

    else:
        all_objects = set()

    return all_objects, on_layout, system_excluded, module_objects


# ---------------------------------------------------------------------------
# Solution discovery
# ---------------------------------------------------------------------------

def discover_solutions():
    """List available solutions in agent/context/."""
    if not CONTEXT_DIR.exists():
        return []
    return [d.name for d in CONTEXT_DIR.iterdir()
            if d.is_dir() and not d.name.startswith(".")]


def resolve_solution(args_solution):
    """Resolve the solution name, auto-selecting if only one exists."""
    if args_solution:
        return args_solution

    solutions = discover_solutions()
    if len(solutions) == 0:
        print("ERROR: No solutions found in agent/context/", file=sys.stderr)
        print("  Run fmcontext.sh first to generate index files.", file=sys.stderr)
        sys.exit(1)
    elif len(solutions) == 1:
        return solutions[0]
    else:
        print("Multiple solutions found. Specify one with -s:", file=sys.stderr)
        for s in sorted(solutions):
            print(f"  {s}", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Cross-reference tracer for FileMaker solutions.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # build
    build_parser = subparsers.add_parser(
        "build", help="Build xref.index from the solution's fm-saxml docs")
    solution_context.add_arguments(build_parser, solution_required=False)
    build_parser.add_argument(
        "--context", default=None,
        help="folder under agent/context/ to write xref.index to, the name query and dead "
             "take with -s (default: -s, or the FileMaker file name when -s is a docs folder "
             "or --file is given)")

    # query
    query_parser = subparsers.add_parser("query", help="Query references")
    query_parser.add_argument("-s", "--solution", help="Solution name")
    query_parser.add_argument(
        "-t", "--type", required=True,
        choices=["field", "script", "layout", "value_list", "custom_func",
                 "table_occurrence"],
        help="Object type to query",
    )
    query_parser.add_argument("-n", "--name", required=True,
                              help="Object name to query")
    query_parser.add_argument(
        "--direction", default="inbound",
        choices=["inbound", "outbound"],
        help="inbound = who references X? (default), outbound = what does X reference?",
    )

    # dead
    dead_parser = subparsers.add_parser("dead", help="Find unreferenced objects")
    dead_parser.add_argument("-s", "--solution", help="Solution name")
    dead_parser.add_argument(
        "-t", "--type", required=True,
        choices=["fields", "scripts", "custom_functions", "layouts", "value_lists"],
        help="Object type to scan",
    )
    dead_parser.add_argument("--verbose", action="store_true",
                             help="Show low-confidence results")

    args = parser.parse_args()
    if args.command == "build":
        cmd_build(args)
        return
    solution = resolve_solution(args.solution)

    if args.command == "query":
        cmd_query(solution, args.type, args.name, args.direction)
    elif args.command == "dead":
        cmd_dead(solution, args.type, args.verbose)


if __name__ == "__main__":
    main()
