#!/usr/bin/env python3
"""
Cross-reference tracer for FileMaker solutions.

Reads the solution's fm-saxml docs (the typed references in references.json,
through solution_context.py) for targeted queries and dead-object detection, and
can write the cross-reference as an index file.

Usage (every command takes -s, plus --docs-root <folder> or $FM_SAXML_DOCS_ROOT;
-s can also be a docs folder; --file <name> picks one file of a multi-file build):
  python3 trace.py query  -s "Solution Name" -t field -n "Clients::Name"
  python3 trace.py query  -s "Solution Name" -t script -n "Print Invoice"
  python3 trace.py dead   -s "Solution Name" -t fields
  python3 trace.py dead   -s "Solution Name" -t scripts
  python3 trace.py dead   -s "Solution Name" -t custom_functions
  python3 trace.py build  -s "Solution Name" [--context <name>]

Output:
  query  → prints references to/from the named object
  dead   → prints unreferenced objects with confidence levels
  build  → writes agent/context/{solution}/xref.index (query and dead don't need it:
           they read the docs each time)
"""

import argparse
import json
import os
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
    "callee": "Perform Script", "target": "step target",
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


def cmd_build(args):
    """Build xref.index for the solution from its fm-saxml docs."""
    ctx, all_refs = load_solution(args)

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


# ---------------------------------------------------------------------------
# Loading a solution (every command)
# ---------------------------------------------------------------------------

def _default_solution(docs_root):
    """The only solution under the docs root, for a command run without -s."""
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


def load_solution(args):
    """(SolutionContext, XRef rows) for the command's -s / --docs-root / --file; exits with one message."""
    if not args.solution:
        args.solution = _default_solution(args.docs_root)
        if not args.solution:
            print("ERROR: no solution given: pass -s <solution> (and --docs-root or "
                  f"${solution_context.DOCS_ROOT_ENV})", file=sys.stderr)
            sys.exit(1)
    try:
        ctx = solution_context.from_args(args)
        return ctx, build_xrefs(ctx)
    except solution_context.SolutionContextError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Query command
# ---------------------------------------------------------------------------

def _is_external(ref):
    """A row whose target is in another file: it never makes a same-named object here used."""
    return "external file" in ref.ref_context


def _other_file_source(doc_id):
    """'xfile:Invoices/scriptStep:Post:0012' -> ('Invoices', 'Post (line 13)')."""
    scope, _, local = doc_id.partition("/")
    file_label = scope.split(":", 1)[1] if scope.startswith(("xfile:", "file:")) else scope
    kind, _, rest = local.partition(":")
    if kind == "scriptStep" and rest.rpartition(":")[2].isdigit():
        script, _, index = rest.rpartition(":")
        return file_label, f"{script} (line {int(index) + 1})"
    if kind == "layoutObject":
        return file_label, rest.rsplit("::", 1)[0]
    return file_label, rest


def other_file_uses(ctx):
    """XRef rows for uses of this file's objects from other files of a multi-file build.

    fm-saxml's backlinks.json holds them (sources scoped "xfile:<folder>/..." or by file);
    references.json, which build reads, only has this file's own references.
    """
    try:
        backlinks = ctx.docs.backlinks
    except (OSError, ValueError):      # no backlinks.json (older or partial docs): nothing to add
        return []
    names = {}
    for prefix, ref_type in (("field", "field"), ("script", "script"), ("layout", "layout"),
                             ("valueList", "value_list"), ("customFunction", "custom_func"),
                             ("to", "table_occurrence")):
        for doc_id, e in ctx.collection(prefix).items():
            names[doc_id] = (ref_type, getattr(e, "qualified_name", None) or e.name)
    rows = []
    for target, links in backlinks.items():
        if target not in names:
            continue
        ref_type, name = names[target]
        for b in links:
            if "/" not in b.source_doc_id or b.relationship_type in STRUCTURAL:
                continue
            file_label, source = _other_file_source(b.source_doc_id)
            role = b.role or ""
            if b.source_entity_type == "relationship":
                location = "sort field" if role.endswith("Sort") else "join field"
            elif b.relationship_type == TRIGGER_RELATIONSHIP and role and role not in LOCATION_FOR_ROLE:
                location = f"trigger: {role}"
            else:
                location = LOCATION_FOR_ROLE.get(role, role or b.relationship_type)
            rows.append(XRef("other_file", f"{file_label}: {source}", location, ref_type, name, ""))
    return rows


def _to_map(ctx):
    """{TO name: base table name}."""
    tables = ctx.collection("table")
    return {to.name: _named(tables.get(to.base_table_doc_id), _doc_name(to.base_table_doc_id or ""))
            for to in ctx.collection("to").values() if getattr(to, "base_table_doc_id", None)}


def cmd_query(args):
    """Query references to/from an object."""
    ctx, xrefs = load_solution(args)
    ref_type, ref_name, direction = args.type, args.name, args.direction
    to_map = _to_map(ctx)

    # Resolve TO-qualified input to canonical form
    canonical_name = ref_name
    if ref_type == "field" and "::" in ref_name:
        parts = ref_name.split("::", 1)
        to_name, field_name = parts[0], parts[1]
        base = to_map.get(to_name, to_name)
        canonical_name = f"{base}::{field_name}"

    if direction == "inbound":
        # Who references this object? (here, and from other files of a multi-file build)
        matches = [x for x in xrefs + other_file_uses(ctx)
                   if x.ref_type == ref_type and x.ref_name == canonical_name and not _is_external(x)]
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
        "file": "FILE",
        "custom_menu": "CUSTOM MENUS",
        "other_file": "OTHER FILES",
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
                note = f" — {ref.ref_context}" if ref.ref_context else ""
                print(f"  {ref.source_name}, {ref.source_location}{note}")
            else:
                note = f" — {ref.ref_context}" if ref.ref_context else ""
                print(f"  {ref.ref_type}: {ref.ref_name} ({ref.source_location}){note}")
        print()

    print(f"Summary: {len(matches)} references across {len(groups)} source type(s)")


# ---------------------------------------------------------------------------
# Dead object scan
# ---------------------------------------------------------------------------

def cmd_dead(args):
    """Find unreferenced objects."""
    ctx, xrefs = load_solution(args)
    obj_type, verbose = args.type, args.verbose
    solution_name = ctx.name

    # Build set of all referenced objects by type: from this file (not its references into
    # other files) and from other files of a multi-file build
    referenced = set()
    for ref in xrefs + other_file_uses(ctx):
        if ref.ref_type == _dead_ref_type(obj_type) and not _is_external(ref):
            referenced.add(ref.ref_name)

    # Build set of all objects of this type
    all_objects, on_layout, system_excluded, module_objects = _get_all_objects(ctx, obj_type, xrefs)

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
            print(f"  {obj} — on layout: {layout_str}")
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


def _get_all_objects(ctx, obj_type, xrefs):
    """Get all objects (from the model), plus layout-only, system-excluded and module objects."""
    system_excluded = set()
    on_layout = {}  # {obj_name: [layout_names]}
    module_objects = {}  # {obj_name: module_label}
    modules = load_modules()
    all_objects = set()

    if obj_type == "fields":
        for f in ctx.collection("field").values():
            canonical = f.qualified_name
            table = canonical.split("::", 1)[0]
            all_objects.add(canonical)

            label = match_module(f.name, "", modules) or match_module(table, "", modules)
            if label:
                module_objects[canonical] = label

            # System exclusions
            storage = getattr(f, "storage", None)
            if f.name in SYSTEM_FIELDS:
                system_excluded.add(canonical)
            elif f.name.startswith("ForeignKey") or f.name.startswith("FK"):
                system_excluded.add(canonical)
            elif storage is not None and storage.global_storage:
                system_excluded.add(canonical)
            elif f.field_type == "Summary":
                system_excluded.add(canonical)

        # Find fields that are only on layouts
        for ref in xrefs:
            if ref.ref_type == "field" and ref.source_type == "layout":
                on_layout.setdefault(ref.ref_name, []).append(
                    ref.source_name.split(" (ID")[0]
                )

    elif obj_type == "scripts":
        for s in ctx.scripts.values():
            all_objects.add(s.name)
            label = match_module(s.name, getattr(s, "folder_path", None) or "", modules)
            if label:
                module_objects[s.name] = label

        # Find scripts only on layouts
        for ref in xrefs:
            if ref.ref_type == "script" and ref.source_type == "layout":
                on_layout.setdefault(ref.ref_name, []).append(
                    ref.source_name.split(" (ID")[0]
                )

    elif obj_type == "custom_functions":
        for cf in ctx.collection("customFunction").values():
            # Skip group / folder headers and separators ("--"): they have no body.
            if not (getattr(cf, "calculation", None) or "").strip():
                continue
            all_objects.add(cf.name)
            label = match_module(cf.name, "", modules)
            if label:
                module_objects[cf.name] = label

    elif obj_type == "layouts":
        for lay in ctx.layouts.values():
            all_objects.add(lay.name)
            label = match_module(lay.name, getattr(lay, "folder_path", None) or "", modules)
            if label:
                module_objects[lay.name] = label

    elif obj_type == "value_lists":
        for vl in ctx.collection("valueList").values():
            all_objects.add(vl.name)
            label = match_module(vl.name, "", modules)
            if label:
                module_objects[vl.name] = label

    return all_objects, on_layout, system_excluded, module_objects


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
        help="folder under agent/context/ to write xref.index to (default: -s, or the "
             "FileMaker file name when -s is a docs folder or --file is given)")

    # query
    query_parser = subparsers.add_parser("query", help="Query references")
    solution_context.add_arguments(query_parser, solution_required=False)
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
    solution_context.add_arguments(dead_parser, solution_required=False)
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
    elif args.command == "query":
        cmd_query(args)
    elif args.command == "dead":
        cmd_dead(args)


if __name__ == "__main__":
    main()
