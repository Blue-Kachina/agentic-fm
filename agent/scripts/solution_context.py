#!/usr/bin/env python3
"""solution_context.py - find a solution's fm-saxml docs and load them, one way for every tool.

The ports off the exploder (trace.py, layout_to_summary.py, extract_theme.py,
fm_xml_to_snippet.py, saxmlpreview.py) read a solution through this module instead of
agent/context/ or agent/xml_parsed/. It wraps fm-saxml's reader API (fm_saxml.load,
find_docs) and turns its failures into one message that says what to run.

    import solution_context as sc

    ctx = sc.load_solution("Invoices")                  # under $FM_SAXML_DOCS_ROOT
    ctx = sc.load_solution("Invoices", docs_root="exports")
    ctx = sc.load_solution("exports/Invoices_docs")     # or a docs folder itself
    ctx = sc.load_solution("Accounting", file="Invoices")   # one file of a multi-file build

    ctx.scripts                     # {docId: script}, likewise layouts, themes
    ctx.pick("script", "Post Invoice")      # by name or docId; clear error on none or several
    ctx.raw_xml("script:Post Invoice")      # the export's own XML for that script
    ctx.refs_to("field:Invoices::Total")

In a tool's command line:

    sc.add_arguments(parser)        # -s/--solution, --docs-root, --file
    ctx = sc.from_args(parser.parse_args())

Check where a solution's docs are found:
    python3 agent/scripts/solution_context.py -s Invoices [--docs-root exports]

Needs fm-saxml (the fm-saxml-converter package) in the running Python. In the DIS harness
that is the .venv made by tools/setup.py. This module itself imports it only when loading,
so a missing install is reported as a message, not a traceback.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

DOCS_ROOT_ENV = "FM_SAXML_DOCS_ROOT"
PACKAGE = "fm-saxml-converter"
INSTALL_HINT = (f"Run this tool with a Python that has fm-saxml: in the DIS harness, the .venv made by "
                f"tools/setup.py (.venv/bin/python3 on Mac, .venv\\Scripts\\python.exe on Windows); "
                f"elsewhere, `python3 -m pip install {PACKAGE}`.")


class SolutionContextError(Exception):
    """A solution's docs can't be found or read. The message says what to run."""


def _fm_saxml():
    try:
        import fm_saxml
    except ImportError as e:
        raise SolutionContextError(f"fm-saxml is not installed for {sys.executable} ({e}).\n{INSTALL_HINT}") from None
    return fm_saxml


def _convert_hint(solution: str) -> str:
    return (f"Convert the solution's Save a Copy as XML with fm-saxml, for example "
            f"`fm-saxml build {solution}.xml --docs-root <docs root>`, then pass --docs-root <docs root> "
            f"or set {DOCS_ROOT_ENV}.")


def _is_docs_folder(path: Path) -> bool:
    return path.is_dir() and ((path / "docs.json").is_file() or (path / "entities.json").is_file()
                              or any((d / "entities.json").is_file() for d in path.iterdir() if d.is_dir()))


def locate(solution: str, docs_root: str | os.PathLike | None = None) -> Path:
    """The docs folder of ``solution``: a docs folder path itself, else fm-saxml's find_docs under the root.

    The root is ``docs_root``, else $FM_SAXML_DOCS_ROOT. Matching (folder name, docs.json
    solution, or a FileMaker file in a batch) and its order are fm-saxml's.
    """
    fm_saxml = _fm_saxml()
    as_path = Path(solution).expanduser()
    if (as_path.is_absolute() or os.sep in solution or "/" in solution) and _is_docs_folder(as_path):
        return as_path
    root = docs_root if docs_root is not None else os.environ.get(DOCS_ROOT_ENV)
    if not root:
        raise SolutionContextError(f"No fm-saxml docs root for {solution!r}: pass --docs-root <folder> or set "
                                   f"{DOCS_ROOT_ENV} to the folder that holds the converted solutions.\n"
                                   + _convert_hint(solution))
    try:
        return fm_saxml.find_docs(solution, root)
    except fm_saxml.DocsNotFoundError as e:
        raise SolutionContextError(f"No fm-saxml docs for {solution!r} under {root}: {e}\n"
                                   + _convert_hint(solution)) from None


def open_docs(solution: str, docs_root: str | os.PathLike | None = None):
    """fm-saxml's own object for the solution: SolutionDocs (one file) or BatchDocs (a multi-file build)."""
    fm_saxml = _fm_saxml()
    path = locate(solution, docs_root)
    try:
        return fm_saxml.load(path)
    except fm_saxml.ContractVersionError as e:
        raise SolutionContextError(f"{e}\nThese docs were written by a newer fm-saxml than {_version()}: "
                                   "upgrade fm-saxml (in the DIS harness: bump requirements.txt, "
                                   "rerun tools/setup.py).") from None
    except fm_saxml.DocsNotFoundError as e:
        raise SolutionContextError(f"{e}\n" + _convert_hint(solution)) from None


def load_solution(solution: str, docs_root: str | os.PathLike | None = None,
                  file: str | None = None) -> "SolutionContext":
    """One FileMaker file's docs, ready to read.

    For a multi-file build, ``file`` picks the file; without it, a batch of one file gives
    that file, and a solution named after one of the batch's files gives that file (fm-saxml's
    find_docs resolves it to the file's folder).
    """
    fm_saxml = _fm_saxml()
    docs = open_docs(solution, docs_root)
    batch = None
    if isinstance(docs, fm_saxml.BatchDocs):
        batch = docs
        names = list(batch)
        if file is None and len(names) == 1:
            file = names[0]
        if file is None:
            raise SolutionContextError(f"{solution!r} is a multi-file build ({batch.path}) with files "
                                       f"{', '.join(names)}: pass --file <name> to pick one.")
        try:
            docs = batch[file]
        except KeyError:
            raise SolutionContextError(f"{file!r} is not a file of {solution!r} ({batch.path}); "
                                       f"its files: {', '.join(names)}.") from None
    elif file is not None and file.casefold() != docs.name.casefold():
        raise SolutionContextError(f"{solution!r} is one file's docs ({docs.name}, {docs.path}), "
                                   f"not a multi-file build with a file {file!r}.")
    ctx = SolutionContext(docs, batch)
    try:
        ctx.schema_version          # reads entities.json now, so a bad or newer file fails here
    except fm_saxml.ContractVersionError as e:
        raise SolutionContextError(f"{e}\nThese docs were written by a newer fm-saxml than {_version()}: "
                                   "upgrade fm-saxml (in the DIS harness: bump requirements.txt, "
                                   "rerun tools/setup.py).") from None
    except fm_saxml.DocsNotFoundError as e:
        raise SolutionContextError(f"{e}\n" + _convert_hint(solution)) from None
    return ctx


def _version() -> str:
    try:
        return _fm_saxml().__version__
    except SolutionContextError:
        return "?"


class SolutionContext:
    """One FileMaker file's docs, with the reads the ports need. ``docs`` is fm-saxml's SolutionDocs."""

    def __init__(self, docs, batch=None) -> None:
        self.docs = docs
        self.batch = batch      # the BatchDocs this file came from, or None

    def __repr__(self) -> str:
        return f"SolutionContext({self.name!r}, {str(self.path)!r})"

    @property
    def path(self) -> Path:
        return self.docs.path

    @property
    def name(self) -> str:
        """The FileMaker file name without extension."""
        return self.docs.name

    @property
    def schema_version(self) -> str:
        return self.docs.schema_version

    @property
    def entities(self):
        return self.docs.entities

    def collection(self, kind: str) -> dict[str, Any]:
        """``{docId: entity}`` of one kind ("script", "layout", "theme", "field", ...)."""
        return self.docs.collection(kind)

    @property
    def scripts(self) -> dict[str, Any]:
        return self.collection("script")

    @property
    def layouts(self) -> dict[str, Any]:
        return self.collection("layout")

    @property
    def themes(self) -> dict[str, Any]:
        return self.collection("theme")

    @property
    def references(self) -> list:
        return self.docs.references

    def refs_from(self, doc_id: str) -> list:
        return self.docs.refs_from(doc_id)

    def refs_to(self, doc_id: str) -> list:
        return self.docs.refs_to(doc_id)

    def get(self, doc_id: str):
        return self.docs.get(doc_id)

    def find(self, kind: str, name: str) -> list:
        """Every entity of ``kind`` named ``name``, ignoring case."""
        return self.docs.find(kind, name)

    def pick(self, kind: str, key: str):
        """The one entity of ``kind`` whose docId or name is ``key``.

        A docId wins over a name. No match, or several, raises SolutionContextError that lists
        the candidates (with docIds, so the caller can pass one).
        """
        entities = self.collection(kind)
        if key in entities:
            return entities[key]
        hits = self.find(kind, key)
        if len(hits) == 1:
            return hits[0]
        if hits:
            listed = "\n".join(f"  {e.doc_id}" for e in hits)
            raise SolutionContextError(f"{len(hits)} {kind}s in {self.name} are named {key!r}; "
                                       f"pass one docId instead:\n{listed}")
        wanted = key.strip().casefold()
        near = sorted(e.doc_id for e in entities.values()
                      if wanted in (getattr(e, "name", "") or "").casefold())[:10]
        hint = ("Did you mean:\n" + "\n".join(f"  {d}" for d in near)) if near else \
            f"{len(entities)} {kind}s in {self.name}."
        raise SolutionContextError(f"No {kind} {key!r} in {self.name} ({self.path}). {hint}")

    def raw_xml(self, doc_id: str) -> str:
        """The scrubbed export XML of a script, step, layout, layout object, theme, custom menu or menu set."""
        fm_saxml = _fm_saxml()
        try:
            return self.docs.raw_xml(doc_id)
        except fm_saxml.RawXmlNotFoundError as e:
            raise SolutionContextError(f"{e}\nRebuild the docs with fm-saxml without --no-raw-xml "
                                       "if the raw/ folder is missing.") from None


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def add_arguments(parser: argparse.ArgumentParser, solution_required: bool = True) -> None:
    """The options every ported tool takes to name a solution."""
    parser.add_argument("-s", "--solution", required=solution_required,
                        help="solution name under the docs root, or a docs folder path")
    parser.add_argument("--docs-root", default=None,
                        help=f"folder holding the converted solutions (default: ${DOCS_ROOT_ENV})")
    parser.add_argument("--file", default=None,
                        help="for a multi-file build: which FileMaker file")


def from_args(args: argparse.Namespace) -> SolutionContext:
    return load_solution(args.solution, args.docs_root, args.file)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Show where a solution's fm-saxml docs are found.")
    add_arguments(ap)
    a = ap.parse_args(argv)
    try:
        docs = open_docs(a.solution, a.docs_root)
        ctx = from_args(a)
    except SolutionContextError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    files = list(docs) if ctx.batch is not None else [ctx.name]
    print(f"Solution:   {a.solution}")
    print(f"Docs:       {docs.path} ({'multi-file build' if ctx.batch is not None else 'one file'})")
    print(f"Files:      {', '.join(files)}")
    print(f"File read:  {ctx.name} ({ctx.path})")
    print(f"Contract:   {ctx.schema_version} (fm-saxml {_version()})")
    print(f"Contents:   {len(ctx.scripts)} scripts, {len(ctx.layouts)} layouts, {len(ctx.themes)} themes, "
          f"{len(ctx.references)} references")
    return 0


if __name__ == "__main__":
    sys.exit(main())
