#!/usr/bin/env python3
"""
Validate fmxmlsnippet files for common errors before pasting into FileMaker.

This script is a backward-compatible shim that delegates to the FMLint engine
(agent/fmlint/). For the full linter with HR format support, additional rules,
and JSON output, use: python3 -m agent.fmlint

Checks:
  1. Well-formed XML
  2. Correct root element (<fmxmlsnippet type="FMObjectList">)
  3. No <Script> wrapper (output should be steps only)
  4. Step attributes (enable, id, name present on every <Step>)
  5. Paired steps balanced and properly nested (If/End If, Loop/End Loop, etc.)
  6. Else/Else If ordering within If blocks
  7. Known step names (cross-referenced against step catalog)
  8. CONTEXT.json cross-reference (field, layout, and script references)
  9. Coding conventions (ASCII comparison operators, variable naming prefixes)

Usage:
  python3 validate_snippet.py [file_or_directory ...] [options]

Examples:
  python3 validate_snippet.py                          # validate all files in agent/sandbox/
  python3 validate_snippet.py agent/sandbox/MyScript   # validate a single file
  python3 validate_snippet.py file1.xml file2.xml      # validate multiple files in one pass
  python3 validate_snippet.py --context agent/CONTEXT.json  # with reference checking
"""

import argparse
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Resolve project root and ensure agent.fmlint is importable
# ---------------------------------------------------------------------------

_script_dir = Path(__file__).resolve().parent
_project_root = _script_dir.parent.parent
sys.path.insert(0, str(_project_root))

from agent.fmlint.engine import LintRunner
from agent.fmlint.config import LintConfig
from agent.fmlint.types import Severity


# ---------------------------------------------------------------------------
# Output formatting (preserves the original validate_snippet.py appearance)
# ---------------------------------------------------------------------------

def _print_result(filepath, lint_result, quiet=False):
    """Print validation results in the classic validate_snippet.py format."""
    print(f"\n{'=' * 60}")
    print(f"  {filepath}")
    print(f"{'=' * 60}")

    errors = []
    warnings = []
    passes = []

    for d in lint_result.diagnostics:
        loc = f"Step {d.line}" if d.line > 0 else ""
        if d.severity == Severity.ERROR:
            errors.append(f"{loc}: {d.message}" if loc else d.message)
        elif d.severity == Severity.WARNING:
            warnings.append(f"{loc}: {d.message}" if loc else d.message)
        # INFO and HINT are not shown in legacy format

    # In legacy mode, show a pass for the major check categories if no issues
    if not any(d.rule_id.startswith("S00") for d in lint_result.diagnostics
               if d.severity == Severity.ERROR):
        passes.append("Well-formed XML")
        passes.append("Correct root element")
        passes.append("No <Script> wrapper")

    step_attr_errors = [d for d in lint_result.diagnostics if d.rule_id == "S004"]
    if not step_attr_errors:
        passes.append("Step attributes OK")

    pair_errors = [d for d in lint_result.diagnostics
                   if d.rule_id in ("S005", "S006", "S007")]
    if not pair_errors:
        passes.append("Paired steps balanced")

    convention_warns = [d for d in lint_result.diagnostics
                        if d.rule_id in ("N001", "N002")]
    if not convention_warns:
        passes.append("Coding conventions OK")

    if not quiet:
        for msg in passes:
            print(f"  PASS  {msg}")

    for msg in warnings:
        print(f"  WARN  {msg}")

    for msg in errors:
        print(f"  FAIL  {msg}")

    total_checks = len(passes) + len(errors)
    if not errors:
        summary = f"PASSED ({total_checks} check(s) passed"
        if warnings:
            summary += f", {len(warnings)} warning(s)"
        summary += ")"
    else:
        summary = f"FAILED ({len(errors)} error(s)"
        if warnings:
            summary += f", {len(warnings)} warning(s)"
        summary += ")"

    print(f"\n  {summary}")

    return len(errors) == 0


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Validate fmxmlsnippet files for common errors"
    )
    parser.add_argument(
        "paths",
        nargs="*",
        default=None,
        help="Files or directories to validate (default: agent/sandbox/)",
    )
    parser.add_argument(
        "--context",
        default=None,
        help="Path to CONTEXT.json for reference validation",
    )
    parser.add_argument(
        "--snippets",
        default=None,
        help="Path to snippet_examples/ directory (legacy, ignored — uses step catalog)",
    )
    parser.add_argument(
        "--quiet", "-q",
        action="store_true",
        help="Only show errors and warnings",
    )

    args = parser.parse_args()

    # Resolve paths with sensible defaults
    targets = [Path(p) for p in args.paths] if args.paths else [_project_root / "agent" / "sandbox"]
    context_path = Path(args.context) if args.context else None

    for target in targets:
        if not target.exists():
            print(f"Error: {target} does not exist")
            sys.exit(1)

    # Build FMLint runner
    config = LintConfig()
    # Disable rules that weren't in the original validate_snippet.py
    # to keep output consistent during transition
    config.disabled_rules = {
        "S009", "S010", "S011",           # new structure rules
        "N003", "N004", "N005", "N006", "N007",  # new naming rules
        "D001", "D002", "D003",           # documentation rules
        "B001", "B002", "B003", "B004", "B005",  # best practice rules
        "C001", "C002", "C003",           # calculation rules
        "C004", "C005",                   # live eval rules
        "R009",                           # scope mismatch stub
    }

    runner = LintRunner(
        project_root=_project_root,
        context_path=context_path,
        config=config,
    )

    # Print preamble
    catalog_count = len(runner.catalog.known_names())
    if catalog_count:
        print(f"Loaded {catalog_count} known step names from step catalog")
    else:
        print("Warning: step catalog not found")

    if runner.context.available:
        ctx_path = context_path or (_project_root / "agent" / "CONTEXT.json")
        print(f"Loaded CONTEXT.json from {ctx_path}")
        # Staleness is now checked by R008 rule within the lint run

    # Collect files
    files = []
    for target in targets:
        if target.is_file():
            files.append(target)
        elif target.is_dir():
            for f in sorted(target.iterdir()):
                if f.is_file() and not f.name.startswith("."):
                    files.append(f)

    if not files:
        print(f"No files found in {', '.join(str(t) for t in targets)}")
        sys.exit(0)

    # Run validation via FMLint
    all_ok = True
    for filepath in files:
        result = runner.lint_file(str(filepath), fmt="xml")
        file_ok = _print_result(filepath, result, args.quiet)
        if not file_ok:
            all_ok = False

    # Summary
    print(f"\n{'─' * 60}")
    print(f"  {len(files)} file(s) validated: ", end="")
    failed_count = sum(1 for _ in [] if False)  # counted inline above
    if all_ok:
        print("ALL PASSED")
    else:
        print("SOME FAILED")
    print()

    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    # Windows pipes and redirects default to the ANSI code page: accented names would garble
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
    main()
