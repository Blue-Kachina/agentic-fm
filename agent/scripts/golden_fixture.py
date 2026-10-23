#!/usr/bin/env python3
"""golden_fixture.py - record and check the exploder-path golden output (roadmap P2-05).

The fixture in agent/fixtures/golden/EverythingBagel/ holds what the exploder path read on a
Mac run: the Save a Copy as XML export (saxml/), the exploded tree (xml_parsed/) and the
fmcontext.sh index files (context/). This script runs today's exploder-path tools on those
inputs and compares their output with golden/, so each port off the exploder can be checked
against known-good output.

    python3 agent/scripts/golden_fixture.py            # check: print differences, exit 1 on any
    python3 agent/scripts/golden_fixture.py --update   # rewrite golden/ from today's tools

The tools find their inputs from their own location (<root>/agent/context, agent/xml_parsed),
so they run from a temporary copy of the repo layout holding the fixture's inputs, never on
this checkout's agent/context or xml_parsed. Output is normalised before it is compared or
written: line endings and that temporary root.

Tests and ports use:
    run_tools()                      -> {golden name: text}
    compare(name, actual, sort_lines=False) -> None, or a unified diff against golden/<name>
    build_docs(dest)                 -> the fixture's export converted with the installed fm-saxml
"""

from __future__ import annotations

import argparse
import difflib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
FIXTURE = REPO_ROOT / "agent" / "fixtures" / "golden" / "EverythingBagel"
GOLDEN = FIXTURE / "golden"
SOLUTION = "EverythingBagel"

# trace.py query: (golden name, type, name, direction). One or more per object type.
QUERIES = [
    ("field_textField", "field", "textField", "inbound"),
    ("field_numberField", "field", "numberField", "inbound"),
    ("script_Just Beep", "script", "Just Beep", "inbound"),
    ("script_OneOfEverything_outbound", "script", "OneOfEverything", "outbound"),
    ("layout_EverythingBagel", "layout", "EverythingBagel", "inbound"),
    ("layout_CustomMenuSetLayout", "layout", "CustomMenuSetLayout", "inbound"),
    ("value_list_HardcodedValueList", "value_list", "HardcodedValueList", "inbound"),
    ("custom_func_Test", "custom_func", "Test", "inbound"),
    ("table_occurrence_EverythingBagel", "table_occurrence", "EverythingBagel", "inbound"),
    ("table_occurrence_selfJoin", "table_occurrence", "EverythingBagel__EVERYTHINGBAGEL__selfJoin", "inbound"),
]
DEAD_TYPES = ["fields", "scripts", "custom_functions", "layouts", "value_lists"]

# Golden files whose line order carries no meaning (compared sorted).
UNORDERED = {"trace/xref.index"}

# Golden files of tools already ported off the exploder: their exploder version is gone, so
# run_tools() no longer makes them and --update keeps them as they are. Each port's own test
# compares its output with them (trace build: test_trace_build.py; trace query and dead:
# test_trace_query_dead.py).
FROZEN = ({"trace/build.txt", "trace/xref.index"}
          | {f"trace/query_{name}.txt" for name, *_ in QUERIES}
          | {f"trace/dead_{kind}.txt" for kind in DEAD_TYPES})


def _shadow_root(tmp: Path) -> Path:
    """A copy of the repo layout the tools expect, holding the fixture's inputs."""
    root = tmp / "root"
    agent = root / "agent"
    shutil.copytree(REPO_ROOT / "agent" / "scripts", agent / "scripts",
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(REPO_ROOT / "agent" / "catalogs", agent / "catalogs")
    (agent / "config").mkdir()
    for example in (REPO_ROOT / "agent" / "config").glob("*.example"):   # module defaults etc.
        shutil.copy2(example, agent / "config" / example.name)
    shutil.copytree(FIXTURE / "xml_parsed", agent / "xml_parsed")
    shutil.copytree(FIXTURE / "context", agent / "context" / SOLUTION)
    return root


def _normalise(text: str, root: Path) -> str:
    text = text.replace("\r\n", "\n")
    for form in {str(root), root.as_posix()}:
        text = text.replace(form, "<root>")
    # paths printed after the root: one separator style on every OS
    return re.sub(r"<root>[^\n\"']*", lambda m: m.group(0).replace("\\", "/"), text)


def _run(root: Path, *args: str) -> str:
    r = subprocess.run([sys.executable, *args], cwd=root, capture_output=True, text=True,
                       encoding="utf-8", env={**os.environ, "PYTHONUTF8": "1"})
    if r.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} exited {r.returncode}:\n{r.stdout}{r.stderr}")
    return r.stdout + r.stderr


def run_tools() -> dict[str, str]:
    """Today's exploder-path output on the fixture, by golden name (relative path in golden/)."""
    out: dict[str, str] = {}
    with tempfile.TemporaryDirectory() as tmp:
        # resolved: the tools print resolved paths (macOS /var -> /private/var, Windows 8.3 names)
        root = _shadow_root(Path(tmp).resolve())
        ctx = root / "agent" / "context" / SOLUTION
        scripts = "agent/scripts/"

        _run(root, scripts + "layout_to_summary.py", "--solution", SOLUTION)
        for p in sorted((ctx / "layouts").glob("*.json")):
            out[f"layout_to_summary/{p.name}"] = p.read_text(encoding="utf-8")

        for p in sorted((root / "agent" / "xml_parsed" / "scripts" / SOLUTION).glob("*.xml")):
            out[f"saxmlpreview/{p.stem}.txt"] = _run(root, scripts + "saxmlpreview.py", str(p))

        return {name: _normalise(text, root) for name, text in out.items()}


def compare(name: str, actual: str, sort_lines: bool | None = None, golden_dir: Path = GOLDEN) -> str | None:
    """None when ``actual`` matches golden/<name>, else a unified diff (golden first).

    ``sort_lines`` compares the lines in sorted order, for output whose order isn't
    meaningful; by default it is on for the names in UNORDERED.
    """
    path = golden_dir / name
    if not path.is_file():
        return f"no golden file {path} (run golden_fixture.py --update)"
    expected = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    actual = actual.replace("\r\n", "\n")
    if sort_lines if sort_lines is not None else name in UNORDERED:
        expected = "\n".join(sorted(expected.splitlines())) + "\n"
        actual = "\n".join(sorted(actual.splitlines())) + "\n"
    if expected == actual:
        return None
    return "".join(difflib.unified_diff(expected.splitlines(keepends=True), actual.splitlines(keepends=True),
                                        f"golden/{name}", f"actual/{name}"))


def build_docs(dest: Path) -> Path:
    """Convert the fixture's export with the installed fm-saxml; return the docs folder."""
    r = subprocess.run([sys.executable, "-m", "fm_saxml", "build", str(FIXTURE / "saxml" / f"{SOLUTION}.xml"),
                        "--docs-root", str(dest), "--solution", SOLUTION],
                       capture_output=True, text=True, encoding="utf-8", env={**os.environ, "PYTHONUTF8": "1"})
    if r.returncode != 0:
        raise RuntimeError(f"fm-saxml build exited {r.returncode}:\n{r.stdout}{r.stderr}")
    return dest / SOLUTION


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--update", action="store_true", help="rewrite golden/ from today's tools")
    a = ap.parse_args()
    outputs = run_tools()
    on_disk = {p.relative_to(GOLDEN).as_posix() for p in GOLDEN.rglob("*") if p.is_file()} if GOLDEN.exists() else set()
    stale = sorted(on_disk - set(outputs) - FROZEN)
    if a.update:
        for name in stale:
            (GOLDEN / name).unlink()
        for name, text in outputs.items():
            path = GOLDEN / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="\n")
        print(f"Wrote {len(outputs)} golden files to {GOLDEN} (kept {len(FROZEN & on_disk)} frozen)")
        return 0
    diffs = [d for name, text in outputs.items() if (d := compare(name, text))]
    diffs += [f"golden/{name} is no longer produced\n" for name in stale]
    for d in diffs:
        print(d)
    print(f"{len(outputs)} outputs, {len(diffs)} differ")
    return 1 if diffs else 0


if __name__ == "__main__":
    sys.exit(main())
