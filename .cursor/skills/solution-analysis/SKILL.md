---
name: solution-analysis
description: Analyze a FileMaker solution and produce a structured profile covering data model, business logic, UI layer, integrations, and health metrics. Reads the reports fm-saxml writes with the solution's docs, so solutions of any size are handled without sending raw XML through the agent. Use when the developer says "analyze solution", "solution overview", "solution analysis", "solution profile", "solution spec", "what does this solution do", "solution summary", or wants a high-level understanding of an entire FileMaker solution.
compatibility: Needs the solution's fm-saxml docs (fm-saxml-converter 0.6.5 or later). No scripts to run.
---

# Solution Analysis

This skill gives a developer (or someone new to a solution) a complete understanding of what the solution does: its data model, scripted business logic, UI coverage, integration points, and health.

The numbers come from fm-saxml, which writes them with the solution's docs on every conversion; nothing needs building. The agent reads those reports and turns them into a narrative. It never reads raw XML.

**Output**: a narrative overview, in chat or as a markdown document when the developer wants one to share.

## Where the reports are

Every fm-saxml docs folder has a `Reports/` folder:

| File | What it contains |
| --- | --- |
| `Reports/profile.md` | The solution profile: tables with data and field types, relationship measures (join operators, multi-predicate, self-joins, cascades on each side, sorted sides), relationship graph (TO degrees, hub TOs, isolated TOs, disconnected tables), scripts (steps, largest scripts, folders, Perform Script links, entry points), layouts by base TO, layout folders, portals, layouts nothing navigates to, value lists by type, external calls by step type, unused candidates |
| `Reports/summary.md` | Entity counts, reference counts (exact, parsed, unresolved, other files), names written in text, warning counts, scrubbing |
| `Reports/warnings.md` | Every warning (empty scripts, unused field candidates, unresolved references...) |
| `Reports/indirection.md` | Places that name an object in text (GetField, Perform Script by name, ExecuteSQL), which a rename breaks |
| `Reports/unresolved-references.md` | References fm-saxml could not resolve |
| `index.md` | File options, external data sources, and links to each kind's list (scripts, layouts, custom functions...) |

A **multi-file build** (several exports converted together) has one folder per file, each with its own `Reports/`, plus a top-level `Reports/batch-summary.md`: files converted, cross-file references, and how each file's external data sources were paired with the other files.

## Workflow

### Step 1: Find the docs

Determine which solution to analyze:

1. If the developer named a solution or a docs folder, use that.
2. Otherwise look under the docs root (`--docs-root` or `$FM_SAXML_DOCS_ROOT`); if there is only one solution, use it; if there are several, list them and ask.

To check a docs folder and see whether it is a multi-file build:

```bash
python3 agent/scripts/solution_context.py -s "{solution or docs folder}" --docs-root <folder>
```

If no docs are found, the developer converts the Save a Copy as XML export with fm-saxml (`solution_context.py` prints the command). After a schema change, they export and convert again; the reports are rebuilt with the docs.

### Step 2: Read the reports

Read `Reports/profile.md` and `Reports/summary.md` in parallel. For a multi-file build, read `Reports/batch-summary.md` first, then each file's `profile.md` and `summary.md`.

Read the rest only when the narrative needs it: `warnings.md` for health details, `indirection.md` before saying anything about renames, `index.md` for data sources and file options, and the list it links for the names of one kind of object (custom functions, say). For large solutions, read the sections you need rather than whole files.

### Step 3: Produce the narrative

Using the reports, produce a narrative covering:

1. **Executive Summary**: what this solution appears to be (CRM, ERP, inventory system, etc.), judged from table names, script folders and integrations. Mention its scale (tables, fields, scripts, layouts).

2. **Data Architecture**: the core entity tables and how they relate. Judge the relationship graph topology (anchor-buoy, star, tiered hub, spider-web, flat) from the graph measures and the TO names, using `references/relationship-graph-topologies.md`; say it is a judgment and what it rests on. Note cascades (a parent-child cascade is usually on the right side) and self-joins.

3. **Business Logic Domains**: walk through the script folders. For each major folder, describe what that functional area does, judged from script names and entry points. Highlight the largest scripts, and scripts many others call, which serve as shared infrastructure.

4. **UI Coverage**: which tables have layouts and which don't. Use the layout and folder names to judge what layouts are for (`references/layout-classifications.md`); say it is a judgment. Call out layouts nothing navigates to and portals that reveal parent-child UI patterns.

5. **Integration Points**: external data sources (`index.md`) and external calls by step type (Insert from URL, Send Mail, Execute SQL, Perform Script on Server, import and export...).

6. **Health Observations**: unused candidates, disconnected tables, empty scripts, unresolved references, names written in text. Frame these as observations, not criticisms: solutions grow organically and some "unused" objects are intentional placeholders. Unused results are candidates only: calls by calculated name, server schedules and `fmp://` URLs don't show up in the docs.

### Step 4: Optionally write a document

If the developer wants a shareable document, write the narrative as markdown, with the report tables it relies on. Save it where the developer's project keeps deliverables about the solution, named `{solution} - solution-profile.md`.

## Multi-file solutions

In a data separation model (UI file + data file) or any multi-file solution:

- Lead with the architecture: which file is the UI file and which holds the data, from `batch-summary.md` (data source pairings) and each file's table and TO counts.
- Note which tables are local UI or utility tables and which are core domain tables in the data file.
- Most TOs in the UI file usually point at the data file; the true entity relationships live in the data file's graph.
- Uses from other files count: a script another file calls is an entry point ("another file" in profile.md), not unused.

## Performance contract

- **Never read raw XML**: the reports hold what this skill needs.
- **Read reports selectively**: for very large solutions, read the sections you need.
- **Parallel tool calls**: batch independent reads.
