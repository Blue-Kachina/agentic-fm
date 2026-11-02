#!/usr/bin/env python3
"""fmdocs.py - links into a solution's fm-saxml docs site, its age, what changed, the active solution.

fm-saxml's HTML viewer (``fm-saxml build --html``, plus ``--webmcp`` for its query tools;
both on by default when the conversion starts from FileMaker) shows every object of a
solution on its own page, and runs the WebMCP query tools on a results page. An agent
can't call those tools itself (they need a browser), but it can give the user a link that
runs them. This module builds those links and the other facts an answer about a solution
needs (roadmap P5-13, P5-17, P5-18, P6-07):

    base      the viewer's address: the companion's viewer server when one runs, else file://
    link      the page of one object, by docId or by type and name
    results   a results page that runs one WebMCP tool with its arguments
    age       when the docs were exported and converted
    active    print or set the active solution (the one used when -s is left out)
    snapshot  keep the current docs as the previous generation, for `changes`
    changes   what changed since the previous generation, with page links

Run from the agentic-fm root:

    python3 agent/scripts/fmdocs.py link -s Invoices --type script --name "Post Invoice"
    python3 agent/scripts/fmdocs.py results -s Invoices --tool find_references \\
        --args '{"docId": "field:Invoices::Total"}'
    python3 agent/scripts/fmdocs.py changes          # the active solution

The docs root is --docs-root, else $FM_SAXML_DOCS_ROOT, else the DIS harness's exports/
folder (as for the companion's /convert). Page paths come from the viewer's catalog
(_viewer/data/<file>/catalog.js), else from the pages' front matter; they are never
rebuilt from names (fm-saxml slugs them and adds -2, -3 on collisions).

The companion calls save_previous() before each conversion and set_active() after a
successful one; both never raise.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import solution_context as sc

ACTIVE_FILE = ".active.json"        # in the docs root
PREVIOUS_DIR = ".previous"          # in the docs root: <docs root>/.previous/<docs folder name>/
JOB_RECORD = "_companion_job.json"  # companion_server.JOB_RECORD_IN_OUTPUT
STALE_DAYS = 7

# The WebMCP tools a results page can run (fm-saxml templates/webmcp.js). Left out:
# get_current_page, show_results and navigate_to (they act on the open page) and the
# page-scoped list_* tools.
RESULT_TOOLS = (
    "describe_schema", "get_solution_overview", "search_entities", "query_entities",
    "find_script_steps", "get_entity", "find_references", "find_unreferenced",
    "find_indirection", "rename_impact", "traverse", "who_can_access", "what_can_access", "query",
)

# entities.json collections that are derived from others, and entity keys that change
# without the object changing (where it sits in the export).
_DIFF_SKIP_COLLECTIONS = {"variables", "indirectionSites"}
_DIFF_SKIP_KEYS = {"sourceXml", "chunkRefs"}
_ENVELOPE = {"schemaVersion", "generator", "source"}


class FmDocsError(Exception):
    """Something the user has to fix; the message says what."""


# -- docs root, docs folder, viewer root ------------------------------------------------

def docs_root(requested: str | None = None) -> str:
    """--docs-root, else $FM_SAXML_DOCS_ROOT, else the harness exports/ folder, else ""."""
    if requested:
        return requested
    env = os.environ.get(sc.DOCS_ROOT_ENV, "")
    if env:
        return os.path.expanduser(env)
    try:
        from companion_server import _resolve_docs_root
    except Exception:       # companion_server can't be imported here: no harness fallback
        return ""
    return _resolve_docs_root(create=False)[0]


def resolve_docs(solution: str | None, root: str | None = None) -> Path:
    """The docs folder of ``solution`` (a name or a path), or of the active solution when None."""
    root = docs_root(root)
    if not solution:
        active = get_active(root)
        if not active:
            raise FmDocsError("No solution given and no active solution: pass -s <solution>, or set one "
                              "with `fmdocs.py active <solution>`.")
        path = Path(active["output_path"])
        if not path.is_dir():
            raise FmDocsError(f"The active solution's docs folder {path} is gone: pass -s <solution>.")
        return path
    try:
        return sc.locate(solution, root or None)
    except sc.SolutionContextError as e:
        raise FmDocsError(str(e)) from None


def viewer_root(docs: Path) -> Path:
    """The folder with the viewer's index.html: ``docs`` itself, or a multi-file build's root."""
    for folder in (docs, docs.parent):
        if (folder / "index.html").is_file() and (folder / "docs.json").is_file():
            return folder
    return docs


def file_of(docs: Path, file: str | None = None) -> str | None:
    """``file``, else the FileMaker file a batch file's folder stands for (``-s FileB``), else None."""
    if file:
        return file
    root = viewer_root(docs)
    return docs.name if root != docs else None


def _catalog_file_keys(root: Path) -> list[str]:
    data = root / "_viewer" / "data"
    return sorted(d.name for d in data.iterdir() if d.is_dir()) if data.is_dir() else []


# -- base URL -------------------------------------------------------------------------

def companion_health() -> dict | None:
    """The running companion's /health, or None."""
    try:
        import companion_control
    except Exception:
        return None
    port = int(os.environ.get("COMPANION_PORT") or 8765)
    return companion_control.fetch_health("127.0.0.1", port, timeout=1.0)


def _same_folder(a: str | os.PathLike, b: str | os.PathLike) -> bool:
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def base_url(docs: Path, health: dict | None | bool = False) -> tuple[str, str]:
    """(URL of the viewer's index.html, "served" | "file"). FmDocsError when there is no viewer.

    ``health`` is the companion's /health (False: ask it now). A viewer the companion
    serves gets its http URL (WebMCP works there); otherwise the file:// URL, where pages
    and results links work but the browser offers no native WebMCP.
    """
    root = viewer_root(docs)
    if not (root / "index.html").is_file():
        raise FmDocsError(f"{root} has no HTML viewer. Convert again with the viewer: from FileMaker "
                          "(on by default), or `fm-saxml build <export> --docs-root <root> --solution <name> "
                          "--webmcp`.")
    if health is False:
        health = companion_health()
    for viewer in (health or {}).get("viewers") or []:
        if viewer.get("url") and viewer.get("output_path") and _same_folder(viewer["output_path"], root):
            return viewer["url"], "served"
    return (root / "index.html").resolve().as_uri(), "file"


def page_url(base: str, page_path: str) -> str:
    """The viewer's own hrefFor(): each path segment URI-encoded."""
    return base + "#/" + "/".join(urllib.parse.quote(part, safe="") for part in page_path.split("/"))


def results_url(base: str, tool: str, args: dict, as_json: bool = False) -> str:
    if tool not in RESULT_TOOLS:
        raise FmDocsError(f"{tool!r} is not a WebMCP tool a results page can run. Tools: {', '.join(RESULT_TOOLS)}.")
    spec = json.dumps({"tool": tool, "args": args}, ensure_ascii=False, separators=(",", ":"))
    # encodeURIComponent: everything but A-Z a-z 0-9 - _ . ! ~ * ' ( )
    return base + "#/results?q=" + urllib.parse.quote(spec, safe="-_.!~*'()") + ("&format=json" if as_json else "")


# -- page index -----------------------------------------------------------------------

_CATALOG_RE = re.compile(r"^FMDocs\.catalog\((\"(?:[^\"\\]|\\.)*\"),(.*)\);\s*$", re.S)


def _read_catalog(path: Path) -> dict:
    m = _CATALOG_RE.match(path.read_text(encoding="utf-8"))
    if not m:
        raise FmDocsError(f"{path} is not an fm-saxml viewer catalog.")
    return json.loads(m.group(2))


def page_index(docs: Path) -> list[dict]:
    """Every named object with a page: [{"docId", "type", "name", "path", "file"}].

    ``path`` is relative to the viewer root; ``file`` is the FileMaker file's key in a
    multi-file build, else "".
    """
    root = viewer_root(docs)
    rows = []
    keys = _catalog_file_keys(root)
    if keys:
        for key in keys:
            catalog = root / "_viewer" / "data" / key / "catalog.js"
            if not catalog.is_file():
                continue
            for doc_id, kind, name, path in _read_catalog(catalog).get("index", []):
                if path:
                    rows.append({"docId": doc_id, "type": kind, "name": name, "path": path,
                                 "file": "" if key == "_" else key})
        return rows
    return _front_matter_index(root)


def _front_matter_index(root: Path) -> list[dict]:
    """The page index from each page's front matter (docs built with --html but no --webmcp)."""
    multi = _is_batch(root)
    rows = []
    for md in sorted(root.rglob("*.md")):
        rel = md.relative_to(root).as_posix()
        if rel.startswith(("_viewer/", "Reports/")) or "/Reports/" in rel:
            continue
        meta = _front_matter(md)
        if meta.get("docId"):
            rows.append({"docId": meta["docId"], "type": meta.get("entityType", ""), "name": meta.get("name", ""),
                         "path": rel, "file": rel.split("/", 1)[0] if multi else ""})
    return rows


def _front_matter(md: Path) -> dict:
    meta = {}
    with md.open(encoding="utf-8") as f:
        if f.readline().strip() != "---":
            return meta
        for line in f:
            if line.strip() == "---":
                break
            key, sep, value = line.partition(":")
            if sep:
                meta[key.strip()] = value.strip()
    return meta


def _is_batch(root: Path) -> bool:
    try:
        return json.loads((root / "docs.json").read_text(encoding="utf-8")).get("kind") == "batch"
    except (OSError, ValueError):
        return False


def find_pages(index: Iterable[dict], doc_id: str | None = None, kind: str | None = None,
               name: str | None = None, file: str | None = None) -> list[dict]:
    """Rows matching a docId, or a type and name (ignoring case), optionally in one file."""
    hits = []
    for row in index:
        if file and row["file"].casefold() != file.casefold():
            continue
        if doc_id is not None:
            if row["docId"] == doc_id:
                hits.append(row)
        elif (row["type"].casefold() == (kind or "").casefold()
              and str(row["name"]).casefold() == (name or "").casefold()):
            hits.append(row)
    return hits


# -- age ------------------------------------------------------------------------------

def _parse_time(text: str) -> datetime | None:
    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def docs_age(docs: Path, now: datetime | None = None) -> dict:
    """{"exported", "converted", "age_days", "stale", "source"}; times are aware datetimes or None.

    The export time is the Save a Copy as XML file's modification time when the companion's
    record in the docs folder still points at it, else the time the conversion was asked
    for (the FileMaker script asks right after exporting). The conversion time is the
    record's finish time, else docs.json's modification time.
    """
    now = now or datetime.now(timezone.utc)
    root = viewer_root(docs)
    record = {}
    try:
        record = json.loads((root / JOB_RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    exported, source = None, ""
    export_path = record.get("export_path") or ""
    if export_path and os.path.exists(export_path):
        exported, source = datetime.fromtimestamp(os.path.getmtime(export_path), timezone.utc), "export file"
    elif _parse_time(record.get("created_at", "")):
        exported, source = _parse_time(record["created_at"]), "conversion request"
    converted = _parse_time(record.get("finished_at", ""))
    if converted is None and (root / "docs.json").is_file():
        converted = datetime.fromtimestamp((root / "docs.json").stat().st_mtime, timezone.utc)
    if exported is None:
        exported, source = converted, "conversion (no export time recorded)"
    age = (now - exported).total_seconds() / 86400 if exported else None
    return {"exported": exported, "converted": converted, "age_days": age,
            "stale": age is not None and age > STALE_DAYS, "source": source}


# -- active solution ------------------------------------------------------------------

def get_active(root: str | None = None) -> dict | None:
    root = docs_root(root)
    if not root:
        return None
    try:
        data = json.loads((Path(root) / ACTIVE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("output_path") else None


def set_active(root: str, solution: str, output_path: str) -> bool:
    """Record the active solution in the docs root. Never raises; False when it couldn't."""
    try:
        path = Path(root) / ACTIVE_FILE
        data = {"solution": solution, "output_path": str(Path(output_path).resolve()),
                "set_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
        return True
    except OSError:
        return False


# -- previous generation and changes --------------------------------------------------

def previous_dir(docs: Path) -> Path:
    root = viewer_root(docs)
    return root.parent / PREVIOUS_DIR / root.name


def _entity_files(root: Path) -> dict[str, Path]:
    """{file key: entities.json}; key "" for a one-file build."""
    if (root / "entities.json").is_file():
        return {"": root / "entities.json"}
    return {d.name: d / "entities.json" for d in sorted(root.iterdir()) if (d / "entities.json").is_file()}


def save_previous(docs: str | os.PathLike) -> Path | None:
    """Keep docs.json and each file's entities.json as the previous generation. Never raises.

    Only what `changes` compares is kept, so the copy stays small. The folder has no
    docs.json at its own level, so fm-saxml's find_docs never mistakes it for a solution.
    """
    try:
        root = viewer_root(Path(docs))
        files = _entity_files(root)
        if not files:
            return None
        dest = previous_dir(root)
        staging = dest.with_name(dest.name + ".tmp")
        shutil.rmtree(staging, ignore_errors=True)
        for key, src in files.items():
            (staging / key).mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, staging / key / "entities.json")
        if (root / "docs.json").is_file():
            shutil.copy2(root / "docs.json", staging / "docs.json")
        record = {"saved_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "from": str(root)}
        try:
            record["age"] = {k: (v.strftime("%Y-%m-%dT%H:%M:%SZ") if isinstance(v, datetime) else v)
                             for k, v in docs_age(root).items()}
        except Exception:
            pass
        (staging / "snapshot.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        shutil.rmtree(dest, ignore_errors=True)
        os.replace(staging, dest)
        return dest
    except Exception:
        return None


def _load_entities(path: Path) -> dict[str, dict]:
    """{docId: entity without the keys that move on their own}, script steps left out."""
    data = json.loads(path.read_text(encoding="utf-8"))
    out = {}
    for name, collection in data.items():
        if name in _ENVELOPE or name in _DIFF_SKIP_COLLECTIONS or not isinstance(collection, dict):
            continue
        for doc_id, entity in collection.items():
            if isinstance(entity, dict):
                out[doc_id] = {k: v for k, v in entity.items() if k not in _DIFF_SKIP_KEYS}
    return out


def _owner(doc_id: str, entity: dict) -> str:
    """The docId a change is reported under: a step's script, else the entity itself."""
    if doc_id.startswith("scriptStep:"):
        return entity.get("scriptDocId") or "script:" + doc_id[len("scriptStep:"):].rsplit(":", 1)[0]
    return doc_id


def _name(entity: dict | None, doc_id: str) -> str:
    return (entity or {}).get("name") or doc_id.split(":", 1)[-1]


def compare(old_root: Path, new_root: Path) -> list[dict]:
    """[{"file", "docId", "type", "name", "change": added|removed|changed, "detail"}] sorted.

    A change to a script step is one "changed" row for its script ("steps"); a script that is
    itself added or removed takes its steps' rows with it.
    """
    old_files, new_files = _entity_files(old_root), _entity_files(new_root)
    rows: dict[tuple, dict] = {}
    for key in sorted(set(old_files) | set(new_files)):
        old = _load_entities(old_files[key]) if key in old_files else {}
        new = _load_entities(new_files[key]) if key in new_files else {}
        for doc_id in sorted(set(old) | set(new)):
            before, after = old.get(doc_id), new.get(doc_id)
            if before == after:
                continue
            owner = _owner(doc_id, after or before)
            if owner != doc_id:
                if owner in old and owner in new:
                    row = rows.get((key, owner))
                    if row is None:
                        entity = new[owner]
                        rows[(key, owner)] = {"file": key, "docId": owner, "type": entity.get("entityType", ""),
                                              "name": _name(entity, owner), "change": "changed", "detail": "steps"}
                    elif "steps" not in row["detail"].split(", "):
                        row["detail"] = ", ".join(filter(None, (row["detail"], "steps")))
                continue
            entity = after or before
            change = "added" if before is None else "removed" if after is None else "changed"
            detail = ""
            if change == "changed":
                detail = ", ".join(sorted(k for k in set(before) | set(after)
                                          if before.get(k) != after.get(k) and k != "modified"))
            rows[(key, doc_id)] ={"file": key, "docId": doc_id, "type": entity.get("entityType", ""),
                                   "name": _name(entity, doc_id), "change": change, "detail": detail}
    order = {"added": 0, "removed": 1, "changed": 2}
    return sorted(rows.values(), key=lambda r: (r["file"], order[r["change"]], r["type"], str(r["name"]).casefold()))


# -- command line ---------------------------------------------------------------------

def _fmt_time(t: datetime | None) -> str:
    return t.astimezone().strftime("%Y-%m-%d %H:%M") if t else "unknown"


def _age_line(age: dict) -> str:
    days = age["age_days"]
    when = "unknown" if days is None else "today" if days < 1 else f"{int(days)} day(s) ago"
    line = f"Docs from {_fmt_time(age['exported'])} ({when}; {age['source']})"
    if age["stale"]:
        line += f". Older than {STALE_DAYS} days: export and convert again before relying on them."
    return line


def _try_base(docs: Path) -> str | None:
    try:
        return base_url(docs)[0]
    except FmDocsError:
        return None


def cmd_base(a) -> int:
    docs = resolve_docs(a.solution, a.docs_root)
    url, kind = base_url(docs)
    print(url)
    if kind == "file":
        print("(opened from disk: pages and results links work; native WebMCP needs the companion's viewer)",
              file=sys.stderr)
    return 0


def cmd_link(a) -> int:
    if not a.doc_id and not (a.type and a.name):
        raise FmDocsError("Give --doc-id, or --type and --name.")
    docs = resolve_docs(a.solution, a.docs_root)
    base, _ = base_url(docs)
    file = file_of(docs, a.file)
    hits = find_pages(page_index(docs), a.doc_id, a.type, a.name, file)
    if not hits:
        what = a.doc_id or f"{a.type} {a.name!r}"
        raise FmDocsError(f"No page for {what} in {viewer_root(docs)}"
                          + (f" (file {file})" if file else "") + ".")
    if len(hits) > 1:
        print(f"{len(hits)} pages match; pass --file or --doc-id:", file=sys.stderr)
    for row in hits:
        print(page_url(base, row["path"]) if len(hits) == 1
              else f"{row['file'] or '-'}\t{row['docId']}\t{page_url(base, row['path'])}")
    return 0 if len(hits) == 1 else 3


def cmd_results(a) -> int:
    try:
        args = json.loads(a.args) if a.args else {}
    except ValueError as e:
        raise FmDocsError(f"--args is not JSON: {e}") from None
    if not isinstance(args, dict):
        raise FmDocsError("--args must be a JSON object.")
    docs = resolve_docs(a.solution, a.docs_root)
    print(results_url(base_url(docs)[0], a.tool, args, a.json))
    return 0


def cmd_age(a) -> int:
    docs = resolve_docs(a.solution, a.docs_root)
    age = docs_age(docs)
    print(_age_line(age))
    print(f"Converted {_fmt_time(age['converted'])}; folder {viewer_root(docs)}")
    return 0


def cmd_active(a) -> int:
    root = docs_root(a.docs_root)
    if a.set_solution:
        if not root:
            raise FmDocsError(f"No docs root: pass --docs-root or set {sc.DOCS_ROOT_ENV}.")
        docs = viewer_root(resolve_docs(a.set_solution, root))
        if not set_active(root, a.set_solution, str(docs)):
            raise FmDocsError(f"Could not write {Path(root) / ACTIVE_FILE}.")
    active = get_active(root)
    if not active:
        print("No active solution. Set one with `fmdocs.py active <solution>`; a conversion through the "
              "companion sets it.")
        return 1
    docs = Path(active["output_path"])
    print(f"Active solution: {active['solution']}")
    print(f"Docs:            {docs}" + ("" if docs.is_dir() else " (missing)"))
    if docs.is_dir():
        print(f"Viewer:          {_try_base(docs) or 'none (built without the HTML viewer)'}")
        print(f"Age:             {_age_line(docs_age(docs))}")
    return 0


def cmd_snapshot(a) -> int:
    docs = resolve_docs(a.solution, a.docs_root)
    dest = save_previous(docs)
    if dest is None:
        raise FmDocsError(f"Could not keep {viewer_root(docs)} as the previous generation.")
    print(f"Kept as the previous generation: {dest}")
    return 0


def cmd_changes(a) -> int:
    located = resolve_docs(a.solution, a.docs_root)
    file = file_of(located, a.file)
    docs = viewer_root(located)
    old = previous_dir(docs)
    if not old.is_dir():
        raise FmDocsError(f"No previous generation of {docs.name} ({old}). The companion keeps one at each "
                          "conversion; by hand, run `fmdocs.py snapshot` before converting again.")
    try:
        snap = json.loads((old / "snapshot.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        snap = {}
    rows = compare(old, docs)
    if file:
        rows = [r for r in rows if r["file"].casefold() == file.casefold()]
    base = _try_base(docs)
    pages = {(r["file"], r["docId"]): r["path"] for r in page_index(docs)} if base else {}
    if a.json:
        for r in rows:
            path = pages.get((r["file"], r["docId"]))
            r["url"] = page_url(base, path) if base and path else ""
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0
    prev = _parse_time((snap.get("age") or {}).get("exported") or snap.get("saved_at") or "")
    print(f"Changes in {docs.name} since the previous docs ({_fmt_time(prev)}). {_age_line(docs_age(docs))}")
    if not rows:
        print("No changes.")
        return 0
    for r in rows:
        where = f"{r['file']}: " if r["file"] else ""
        path = pages.get((r["file"], r["docId"]))
        url = f"  {page_url(base, path)}" if base and path and r["change"] != "removed" else ""
        detail = f" ({r['detail']})" if r["detail"] else ""
        print(f"{r['change']:<8} {where}{r['type']} {r['name']}{detail}{url}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Links into a solution's fm-saxml docs site, and facts about its docs.")
    sub = ap.add_subparsers(dest="command", required=True)

    def solution_args(p, file=False):
        p.add_argument("-s", "--solution", default=None,
                       help="solution name under the docs root, or a docs folder (default: the active solution)")
        p.add_argument("--docs-root", default=None,
                       help=f"folder holding the converted solutions (default: ${sc.DOCS_ROOT_ENV}, "
                            "else the harness exports/)")
        if file:
            p.add_argument("--file", default=None, help="for a multi-file build: which FileMaker file")
        return p

    solution_args(sub.add_parser("base", help="the viewer's address")).set_defaults(run=cmd_base)
    p = solution_args(sub.add_parser("link", help="the page of one object"), file=True)
    p.add_argument("--doc-id", help='docId, e.g. "script:Post Invoice"')
    p.add_argument("--type", help="entity type (script, field, layout, tableOccurrence, valueList, ...)")
    p.add_argument("--name", help="object name (a field's name without its table)")
    p.set_defaults(run=cmd_link)
    p = solution_args(sub.add_parser("results", help="a results page that runs one WebMCP tool"))
    p.add_argument("--tool", required=True, help=f"one of: {', '.join(RESULT_TOOLS)}")
    p.add_argument("--args", default="{}", help="the tool's arguments as a JSON object")
    p.add_argument("--json", action="store_true", help="link the JSON form of the result (&format=json)")
    p.set_defaults(run=cmd_results)
    solution_args(sub.add_parser("age", help="when the docs were exported and converted")).set_defaults(run=cmd_age)
    p = sub.add_parser("active", help="print or set the active solution")
    p.add_argument("set_solution", nargs="?", help="solution to make active")
    p.add_argument("--docs-root", default=None)
    p.set_defaults(run=cmd_active)
    solution_args(sub.add_parser("snapshot", help="keep the current docs as the previous generation")
                  ).set_defaults(run=cmd_snapshot)
    p = solution_args(sub.add_parser("changes", help="what changed since the previous generation"), file=True)
    p.add_argument("--json", action="store_true", help="the changes as JSON, with page URLs")
    p.set_defaults(run=cmd_changes)
    return ap


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    try:
        return a.run(a)
    except FmDocsError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
