# Plug-in Integration — routing between the OSS toolchain and the AgenticFM plug-in

Authoritative guide for an AI agent working in the open-source `agentic-fm` project that **may or may not** also have the commercial AgenticFM plug-in installed.
Read this when the session-start probe (see `AGENTS.md` → "Plug-in detection") reports `plugin.usable == true`.

The open-source project is complete on its own.
The plug-in is a strict enhancement — faster and more reliable because it runs in-process against the live FileMaker engine — never a prerequisite.
Every plug-in-aware path here is additive and gated behind detection; removing the plug-in never breaks the OSS workflow.

---

## 1. The one-sentence distinction

The plug-in does in-process, over HTTP, against the live FileMaker engine what the open source does out-of-process, over files and the clipboard.
Where both can do a job, prefer the plug-in **only when it is `usable`** (see §2).

| Job | Open source (`agentic-fm`) | Plug-in (`AgenticFM`) |
|---|---|---|
| **Understand the solution** | Explode a *Save As XML* export into `agent/xml_parsed/`; grep `agent/context/{solution}/*.index` + `agent/CONTEXT.json`. | Parses the same export into an indexed catalog with a cross-reference graph; runs indexed queries. |
| **Generate scripts** | Hand-author `fmxmlsnippet` XML per the catalog rules. | LLM emits human-readable (HR) `fm` blocks; the plug-in converts HR→XML and verifies calcs against the live engine. |
| **Deploy into FileMaker** | `clipboard.py` → `deploy.py` Tier 1/2/3 (manual ⌘V / Ctrl+V, or MBS/AppleScript on a Mac) via the companion server (`:8765`). | Pastes directly into Script Workspace via the embedded HTTP server inside FileMaker. No keystrokes. |
| **Validate** | `python3 -m agent.fmlint` (static). | Static **plus** live-engine verification. |
| **Resolve IDs / context** | Read `CONTEXT.json` + `*.index` (static, can go stale). | Resolved live via the FM engine. |

---

## 2. Detection — "is the plug-in *usable*, and for which solution?"

**Installed ≠ usable.**
The plug-in's HTTP server binds regardless of license state, so file existence and a bare ping prove only *installed*.

> **USABLE** = installed **and** server reachable **and** license `status ∈ {active, trial}`.

The companion server is the single detection broker.
One call resolves everything:

```bash
curl -s --max-time 5 http://local.hub:8765/health
```

Read the `plugin` block:

```jsonc
{
  "status": "ok",
  "plugin": {
    "installed": true,
    "usable": true,                       // the ONLY field you route on
    "server": { "reachable": true, "base": "http://127.0.0.1:8766", "token": "..." },
    "license": { "status": "trial", "daysRemaining": 13 },
    "discover": { /* live, license-gated endpoint suite — see below */ },
    "solutions": [
      { "key": "<uuid>", "name": "CustomApp", "catalog": "/Users/.../<key>/",
        "files": ["CustomApp.fmp12"], "parsed_at": "..." }
    ]
  }
}
```

The companion derives this by stat-ing the plug-in's macOS Application Support tree (the *installed?* precondition), reading the address + bearer token published in `preferences.json`, then probing the plug-in's token-free `GET /api/health` for the `licensed` / `licenseStatus` verdict.
The verdict is cached ~60 s, so repeated `/health` hits are cheap.

**Choose endpoints from the live suite — `/api/discover` is authoritative.**
Do not assume a fixed set of endpoints.
The companion brokers the plug-in's self-describing `GET /api/discover` into `plugin.discover` — the live, license-gated catalog of everything the plug-in currently offers (the full suite when usable; a shrunk list + `purchaseUrl` when locked).
Read it and pick the endpoint that fits the task at hand.
The tables in §3 and §4 below are an **illustrative orientation**, not the contract — when `discover` advertises a capability the tables don't mention (for example, reading a script and then generating + applying an **fmpatch** entirely through endpoints), prefer what `discover` reports.
This keeps the integration from hardcoding — or rotting against — the plug-in's evolving API surface.

**The seam.**
The companion owns and implements the OSS surface (explode, context, clipboard, trigger, static `fmlint`).
It does **not** reimplement plug-in capabilities — it *advertises* them via `plugin.discover` and can optionally proxy them.
What the plug-in "owns" is therefore not a fixed list in this repo; it is whatever `/api/discover` reports at runtime.
The plug-in's API can evolve freely; the OSS only ever learns about it through the broker.
The three coexistence states fall out of this directly, and are the first branches of the decision tree in §3: companion unreachable (pure OSS, manual paste) → plug-in absent or not usable (pure OSS automation tiers) → plug-in usable (plugin-preferred mode).

**Reaching the plug-in.**
Direct access is the baseline: once you have `plugin.server.base` + `token`, call the plug-in's API directly with `Authorization: Bearer <token>`.
For constrained environments (e.g. a container that can reach the companion but not the plug-in's port), the companion also exposes a thin pass-through: `GET|POST {companion}/plugin/<path>` forwards to the plug-in's `/<path>` with the token injected, and returns `502` when the plug-in is not usable.

**Matching the solution.**
Only route understanding/context to the plug-in when `plugin.solutions[]` contains a catalog matching the solution you are working on — match `CONTEXT.json.solution` (or the active `automation.json` solution name) against `solutions[].name`.
If `preferences.json.repoPath` points at this clone, the plug-in is bound to this workspace and the name match can be skipped.
When no catalog matches, use the OSS path (or offer to trigger a fresh index on the plug-in side).

---

## 3. Routing / precedence decision tree

The short rule. Apply it once at session start, then per operation.

```
At session start (once):
  GET {companion}/health
  ├─ companion unreachable          → OSS standalone, no automation; Tier-1 manual paste guidance.
  ├─ plugin absent (!installed)     → OSS path for everything (companion automation tiers as configured).
  ├─ installed but NOT usable       → OSS path for everything; optional one-time nudge:
  │   (plugin.usable == false:        • license expired/revoked → lapsed-license nudge (§6)
  │    server down, or license          • capability routes would 403 license_required anyway —
  │    not active/trial)                  the plug-in enforces it; never route to the plug-in here.
  └─ plugin.usable == true          → PLUGIN-PREFERRED MODE (below).

PLUGIN-PREFERRED MODE — read plugin.discover, then pick the endpoint that fits.
  The lines below are an ILLUSTRATIVE orientation; /api/discover is authoritative.
  Each OSS fallback in [brackets] is what to use if the plug-in path fails or the
  capability is not advertised.
  Understand solution / refs / impact / orphans / analysis
     → discovery endpoints (e.g. /api/discovery/*)  [fallback: trace.py / xml_parsed grep]
  Resolve IDs / context
     → live context endpoint (e.g. /api/context)    [fallback: CONTEXT.json + *.index]
  Author a script
     → emit HR; convert via the advertised HR→XML    [fallback: hand-author fmxmlsnippet]
  Validate
     → validate-HR + live eval endpoints             [fallback: python3 -m agent.fmlint]
  Install / modify a script
     → choose from the advertised script-write suite  [fallback: clipboard.py + deploy.py tiers]
       (create new, insert steps, or read-then-fmpatch — whatever discover offers)
  Run / debug
     → perform-script + eval endpoints               [fallback: deploy.py /trigger]
  Custom menu UUIDs            → menu-lookup skill (OSS, both modes)
  Step structure lookup        → step-catalog grep  (OSS, both modes)

  On ANY plugin call failure (unreachable, 403 license_required, 409 target-drift, 5xx):
     → log once, fall back to the OSS path for that operation, continue.
     → a 403 license_required mid-session means the license lapsed since detection:
       drop to OSS path for the rest of the session and nudge once.
```

Three rules keep this safe:

- **Gate on `usable`, not `installed`.**
  Route only on the resolved `usable` verdict, never on file existence.
  The plug-in backstops this — an unusable plug-in returns `403 license_required` on every capability route, so even a mistaken route degrades cleanly rather than doing fake work.
- **Fall back, never fail.**
  A plug-in error — including a `403 license_required` if the license lapses mid-session — degrades to the OSS path; it never aborts the task.
- **Match the solution first.**
  Only route understanding/context to the plug-in when `plugin.solutions[]` has a catalog for the current solution.

---

## 4. Capability overlap matrix — which tool, when

**Illustrative orientation only — the live, authoritative suite is `plugin.discover` (`GET /api/discover`).**
The plug-in endpoints named below are representative examples; always reconcile against what `discover` actually advertises before calling.
"Plug-in path" assumes the `usable` verdict has been confirmed.

| Operation | OSS path (always available) | Plug-in path (preferred when usable) | Why prefer plug-in |
|---|---|---|---|
| Look up a field/script/layout ID | `CONTEXT.json` → `*.index` grep | `/api/context`, `/api/discovery/entity/:type/:name` | Live, never stale; no export step |
| "Where is X used?" / impact of rename | `trace.py` over `xref.index` | `/api/discovery/references`, `/dependencies`, `/impact` | Indexed graph + BFS blast-radius vs. full-file parse |
| Find orphans / dead objects | `trace.py` dead scan | `/api/discovery/orphans` | Indexed query |
| Solution-wide analysis | `solution-analysis` skill over exploded XML | `/api/discovery/query` (health/security/perf/duplicates/folder/spelling) | Indexed query vs. multi-MB grep |
| Author a script | Hand-write `fmxmlsnippet` from step catalog | Emit HR `fm` block → `/api/hr-to-xml` | Agent writes what the developer reads; conversion + ID resolution is the plug-in's job |
| Validate a script | `python3 -m agent.fmlint` | `/api/validate-hr` + `/api/eval` (live calc verify) | Catches errors against the developer's actual FM version |
| Install / modify a script | `clipboard.py write` → `deploy.py` Tier 1/2/3 → (often) manual ⌘V / Ctrl+V (always on Windows) | the advertised script-write suite — create new, insert steps, or read-then-`fmpatch` | Zero keystrokes, precise line targeting, no clipboard pollution |
| Run / debug a script | the companion's `/trigger` (an `fmp://` URL, or AppleScript on a Mac) | `/api/performscript` + `/api/eval` | Closed-loop over HTTP, single-flight, target-drift guarded |
| Move objects via clipboard | `clipboard.py` (binary class detect) | `/api/clipboard/*` (snapshot store, suspend/resume gate) | Preserves the developer's own clipboard history |

### No-advantage exceptions — keep the OSS path even when the plug-in is present

Documenting these prevents over-routing:

| Operation | Path | Why |
|---|---|---|
| Custom menu UUIDs | `menu-lookup` skill over `xml_parsed/custom_menus/` | Plug-in does **not** auto-resolve these — no advantage |
| Step structure reference | Grep `step-catalog-en.json` | Equivalent knowledge; the OSS path is fine for the CLI agent's own authoring |

---

## 5. Format strategy — fmxmlsnippet vs HR

- **OSS standalone:** unchanged.
  Hand-author `fmxmlsnippet` XML per the strict `AGENTS.md` rules (no XML comments, exact `<Step id>` structure, catalog-driven).
- **Plug-in `usable`:** prefer **HR `fm` blocks** and let the plug-in convert + verify.
  This is lighter on tokens and matches the format the developer reads.
  The plug-in's converter is catalog-driven by the **same** `step-catalog-en.json` family the OSS uses, and the plug-in syncs that catalog from this repo — so HR↔XML fidelity matches what you would hand-author.
- **Never** rip `fmxmlsnippet` authoring out of the OSS agent.
  It stays the standalone path and the universal fallback; HR-via-plug-in is the accelerated path, not a replacement.

---

## 6. Lapsed-license nudge (gentle, never nag)

When detection reports `installed == true` but `usable == false` because the trial/license has lapsed (`license.status ∈ {expired, revoked}`), you may surface a single non-blocking line **once per session**:

> *Detected the AgenticFM plug-in, but its trial/license has lapsed.
> Renewing re-enables the fast path (live Discovery, direct script install, HR authoring).
> Continuing on the open-source workflow for now.*

Rules:

- At most once per session; never blocks; never degrades the OSS work.
- The plug-in exposes a `purchaseUrl` in its locked discovery response — surface that link.
- Do **not** nudge when the plug-in is simply **absent** (`!installed`) — that user has not opted in.

---

## 7. Non-goals / invariants

- **Do not** make any OSS feature depend on the plug-in.
- **Do not** treat "installed" as "usable" — route only on the live `usable` verdict.
- **Do not** make plug-in routing depend on the companion proxy — direct access is the baseline; the proxy is an optional convenience.
- **Do not** strip `fmxmlsnippet` authoring from the OSS agent.
- **Do not** auto-trigger plug-in actions that mutate FileMaker without the confirmation discipline the OSS path already requires.
  The plug-in enforces its own mutation gates (surfaced in `plugin.gates` when present) — respect those rather than stacking a redundant second confirmation.
- **Do not** let a plug-in error abort an OSS task — always fall back.
