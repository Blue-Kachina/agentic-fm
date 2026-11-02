# Golden fixture: exploder-path output on EverythingBagel

What the exploder path (Explode XML → `fmparse.sh` → fm-xml-export-exploder → `fmcontext.sh`)
reads and what today's tools make of it, on the EverythingBagel test file. Each port off the
exploder (roadmap Phase 2) compares its output with `EverythingBagel/golden/`, or explains
each difference.

## EverythingBagel/

| Folder | What | Made by |
|---|---|---|
| `saxml/EverythingBagel.xml` | Save a Copy as XML of the file | FileMaker Pro, on the Mac run below |
| `xml_parsed/` | the exploded tree (78 files) | fm-xml-export-exploder, on the Mac run |
| `context/` | `fields`, `layouts`, `relationships`, `scripts`, `table_occurrences`, `value_lists`, `custom_functions` `.index` | `fmcontext.sh`, on the Mac run |
| `golden/` | tool output on those inputs (below) | `agent/scripts/golden_fixture.py --update` |

The fm-saxml side is not stored: tests convert `saxml/EverythingBagel.xml` with the installed
fm-saxml (`golden_fixture.build_docs()`), so it always matches the version under test. The
fork's CI pins fm-saxml-converter 0.6.12.

### The Mac run (inputs)

Copied unchanged from `samples/EverythingBagel_mac_run/` in fm-saxml-converter
(<https://github.com/Blue-Kachina/fm-doc-saxml>, commit 3c3c4a8, MIT licence, copyright (c) 2026
Matthew Leering), whose `NOTES.md` has the full record. Run on 2026-10-21:

| Item | Version |
|---|---|
| FileMaker Pro | 26.0.3; SaXML `FMSaveAsXML version="2.3.0.0"` |
| fm-xml-export-exploder | 0.6.1 (sha256 `03efac61602973b63a19452dec3bc0f110895f908c83aa6e5b8b1447d6beef03`) |
| agentic-fm | upstream petrowsky/agentic-fm `b2669b6` (version.txt 1.0.0), clean tree |
| macOS, Python | 26.5.2 arm64, Python 3.14.7 |

How it was run: a copy of EverythingBagel.fmp12 (same file name) got two scripts pasted from
`filemaker/agentic-fm.xml`, "Get agentic-fm path" and "Explode XML" (so the export has them as
script IDs 3 and 4); with `companion_server.py` running, Explode XML did Save a Copy as XML and
POSTed to `/explode`, and `fmparse.sh` ran with its defaults (`--output_tree domain`, no
`--all-lines`, no `--lossless`, no `agent/config/*.json`).

### golden/ (outputs)

Made by this fork's tools before they were ported, run by `golden_fixture.py` from a temporary
copy of the repo layout that holds the inputs above. Every one is ported now (P2-06 to P2-12), so
the files are frozen (below) and `golden_fixture.py` no longer runs any tool:

| Folder | Commands |
|---|---|
| `layout_to_summary/` | `layout_to_summary.py --solution EverythingBagel` |
| `trace/` | `trace.py build`; `trace.py query` for each type (list in `QUERIES`); `trace.py dead` for each type; the built `xref.index` (compared ignoring line order) |
| `saxmlpreview/` | `saxmlpreview.py` on each `xml_parsed/scripts/` file: the line map |
| `extract_theme/` | `extract_theme.py` on each custom theme: sha256 of `theme.css`, `theme-web.css`, `theme-classes.json` and `theme-manifest.json` (that one key-sorted), recorded at the port (P2-10) |

Differences from the Mac run's own default path, on purpose:

- Layout summaries are built first. The default path skips them, and then `trace.py` warns
  that layout references are missing and its dead scans are unreliable (every field and both
  layouts came out "unused").
- Output is normalised: LF line endings, the temporary root as `<root>`, and `/` in paths after
  it.

Checked on 2026-10-23: run the default way (no layout summaries), this fork's tools on Windows
gave the Mac run's outputs exactly (trace build, the five dead scans, `xref.index`, and the
analyze profile in all three formats), apart from timestamps and absolute paths.

### Known exploder-path bugs kept in the golden

Ports are expected to differ here; say so when comparing.

1. `context/custom_functions.index` is empty: exploder 0.6.1 writes no `custom_function_stubs/`,
   which `fmcontext.sh` reads. `trace.py` still counts 1 custom function.
2. `context/value_lists.index` has empty names and IDs: `fmcontext.sh` reads
   `/ValueList/ValueListReference/@name`, but the exploded files have `/ValueList/@name`. So
   `trace/query_value_list_HardcodedValueList.txt` finds nothing and `dead_value_lists.txt`
   lists a blank name.

## Use

```bash
python3 agent/scripts/golden_fixture.py            # check today's tools against golden/
python3 agent/scripts/golden_fixture.py --update   # rewrite golden/ (review the diff)
```

In a port's test: `golden_fixture.compare("trace/xref.index", new_output)` returns None or a
unified diff; `sort_lines=True` ignores line order. `test_golden_fixture.py` checks that today's
tools still reproduce `golden/`. Once a tool is ported, its golden files are listed in
`golden_fixture.FROZEN`: they stay as the reference, `--update` leaves them alone, and the port's
own test compares with them.

| Ported | Frozen golden files | Compared by |
|---|---|---|
| `trace.py build` (P2-06) | `trace/build.txt`, `trace/xref.index` | `test_trace_build.py` (differences listed with reasons in `GOLDEN_DIFFERENCES`) |
| `trace.py query` and `dead` (P2-07) | `trace/query_*.txt`, `trace/dead_*.txt` | `test_trace_query_dead.py` (changed outputs and reasons in `GOLDEN_CHANGES`) |
| `layout_to_summary.py` (P2-09) | `layout_to_summary/*.json` | `test_layout_to_summary.py` (byte-identical, no differences) |
| `saxmlpreview.py` input (P2-12) | `saxmlpreview/*.txt` | `test_script_input.py` (identical from unscrubbed docs; from scrubbed docs, two lines of OneOfEverything differ (redacted credentials), listed in `SCRUBBED_LINES`) |
| `extract_theme.py` (P2-10) | `extract_theme/*.sha256` | `test_extract_theme.py` (same four files; the manifest's color palette is in swatch order, so its sum is taken key-sorted) |

`analyze.py` was deleted, not ported (P2-14), and its golden profile with it: fm-saxml's
`Reports/profile.md` replaces it. The comparison is in the Phase 2 roadmap, unit P2-08.
