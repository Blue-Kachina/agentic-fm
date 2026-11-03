# The docs site in answers

How an agent points the user at fm-saxml's docs site when it answers a question about a solution (roadmap P5-13, P5-14, P5-17, P5-18, P6-07). Every skill that answers about a solution's objects follows this page.

## What the site is

`fm-saxml build --html` writes an HTML viewer next to the solution's docs (`index.html`); `--webmcp` adds the WebMCP query tools. A conversion started from FileMaker builds both. The viewer shows every object on its own page (steps, Used By, references, back-links) and runs any query tool on a results page.

The agent can't call the WebMCP tools: they run in the browser. It works out the answer from the docs files (`solution_context.py`, `trace.py`, the pages) and gives the user a link to the same answer in the site, where they can check it and look around.

| Where | URL |
| --- | --- |
| The companion serves the viewer (after a conversion with WebMCP) | `http://127.0.0.1:8766/index.html` (WebMCP works in the browser) |
| Otherwise | `file:///<docs folder>/index.html` (pages and results links work; no native WebMCP) |

## `fmdocs.py`

Run from the agentic-fm root. `-s` takes a solution name or a docs folder; without it, the **active solution** is used (the last one converted through the companion, or set with `active`). `--docs-root` defaults to `$FM_SAXML_DOCS_ROOT`, else the DIS harness's `exports/`.

```bash
python3 agent/scripts/fmdocs.py link -s Invoices --type script --name "Post Invoice"
python3 agent/scripts/fmdocs.py link -s Invoices --doc-id "field:Invoices::Total"
python3 agent/scripts/fmdocs.py results -s Invoices --tool find_references --args '{"docId": "field:Invoices::Total"}'
python3 agent/scripts/fmdocs.py age -s Invoices        # "Docs from <date> (...)", with a warning past 7 days
python3 agent/scripts/fmdocs.py changes -s Invoices    # since the previous conversion, with page links
python3 agent/scripts/fmdocs.py active [Invoices]      # print or set the active solution
python3 agent/scripts/fmdocs.py base -s Invoices       # the viewer's address
```

- `link` exits 3 and lists the candidates when a name matches several pages (a multi-file build): pass `--file` or `--doc-id`. Field names go without their table (`--type field --name Total`); the docId has the table.
- Page paths come from the viewer's own index; never build a URL from an object's name (fm-saxml changes names to make file names, and adds `-2` on collisions).
- A script step has no URL of its own: link the script's page and give the step number.
- `changes` needs a previous generation. The companion keeps one at each conversion (`<docs root>/.previous/<solution>/`). After a hand-run `fm-saxml build`, run `fmdocs.py snapshot` before the next build.

## Answer rules

1. **Name an object, link its page.** Each script, field, layout, table occurrence, value list or custom function named in an answer gets its page link (`fmdocs.py link`), as a Markdown link on its name.
2. **Many results, link the list.** When the answer comes from a question in the table below and the matching call returns more than three results, the answer ends with "See all in the docs: <link>" (`fmdocs.py results` with that call). Count what the call returns (references, objects, steps), not how the answer groups them: a field used by five layout objects, one relationship and one value list has seven results, not three.
3. **Say how old the docs are.** Once per answer, give the `fmdocs.py age` line. Past 7 days, say the answer may be out of date and suggest exporting again.
4. **No viewer, no links.** When `fmdocs.py` reports "no HTML viewer", answer without links and say once that converting from FileMaker (or with `--webmcp`) adds them.
5. **List the links used** in the answer's closing notes, under "Docs links".

## Which tool answers which question

Use the call in the results link; give the agent's own answer in the text. In a multi-file build, add `"file": "<FileMaker file>"` to scope a call to one file.

| Question | Tool and arguments |
| --- | --- |
| Where is X used? What breaks if I rename X? | `find_references {"docId": "<docId>"}`, or `rename_impact {"docId": "<docId>"}` |
| References to any field of table T | `find_references {"targetType": "field", "parentDocId": "table:T", "direction": "incoming", "groupBy": "target"}` |
| Unused scripts, fields, value lists... (candidates only) | `find_unreferenced {"type": "script"}` |
| Find by name or text | `search_entities {"query": "<text>", "deep": true}` |
| Steps of one kind, or that set field F | `find_script_steps {"stepName": "Set Field", "targetDocId": "field:T::F"}` |
| Everything that calls, or is called by, script S | `traverse {"from": "script:S", "direction": "incoming", "relationshipTypes": ["usesScript", "triggersScript"]}` |
| Objects with a property (global fields, triggers, layouts on a TO...) | `query_entities {"type": "field", "where": [{"field": "global", "op": "eq", "value": true}]}` |
| Everything changed since a date, in any type, newest first | `query_entities {"where": [{"field": "modified.timestamp", "op": "gte", "value": "<YYYY-MM-DD>"}], "fields": ["modified.timestamp", "modified.accountName"], "sort": {"field": "modified.timestamp", "desc": true}}` (no `type` searches every type; types without a stamp are listed in `skippedTypes`) |
| Who can run, view or edit X? | `who_can_access {"docId": "<docId>", "action": "run"}` |
| What can account A or privilege set P do? | `what_can_access {"account": "A", "type": "script"}` |

The viewer's `#/agents` page lists every tool with its arguments and example links. fm-saxml's `tests/webmcp_questions.md` has 30 more questions with their calls.

## Explaining one object

For "tell me about script / field / layout X": find the page with `fmdocs.py link`, read that page's `.md` in the docs folder (the same path the link ends with), answer from it, and link it. Reviews, refactors and debugging go to their own skills.

## What changed

For "what changed since the last export" or "did my deploy land": the user exports and converts again from FileMaker, then `fmdocs.py changes` lists what was added, removed and changed. A changed script step shows as its script, with "steps". For a deploy, check the deployed script is listed as changed (or added) and nothing else changed by accident.
