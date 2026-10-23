# FileMaker Relationship Graph Topology Classifications

## Why FileMaker Is Distinct

In SQL-based systems, relationships exist at the schema level — they define how tables join and constrain data integrity. The application layer (views, controllers, queries) is separate. In FileMaker, the **relationship graph** conflates three concerns into a single visual structure:

1. **Schema (Model)** — True entity relationships: primary keys, foreign keys, referential integrity. These are the relationships that would appear in a traditional ERD.

2. **Navigation (View)** — Table occurrences created to drive layouts, portals, and filtered views. A base table may have 15+ TOs, each providing a different filtered or contextual window into the same data for different UI purposes.

3. **Logic (Controller)** — Utility relationships that implement workflow mechanics: global-to-record joins for session state, cartesian joins for value lists, self-joins for hierarchical navigation, and temporary-key joins for scripted operations.

Understanding which TOs serve which purpose is essential to reading an unfamiliar solution's graph. The topology classification identifies the **management strategy** the developer used to organize these three concerns.

## Classification Framework

### Primary Topology: Anchor-Buoy

**Structure**: One designated "anchor" TO per base table serves as the primary entry point. All other TOs for that table ("buoys") connect through the anchor with degree 1, creating a strict tree structure radiating outward from each anchor.

**Distinguishing signals**:

- Very high percentage of degree-1 TOs (80%+)
- Hub count roughly proportional to base table count (one hub per table)
- Maximum hub degree typically 5-25 (proportional to how many related entities each table touches)
- Naming convention encodes the anchor relationship: `anchor__TABLE__joinField__modifier` or similar hierarchical naming
- Multi-level nesting: `order__order_line_item__PRODUCT__productID` creates 3+ level drill-down paths
- Suffixes encode cascade semantics: `__cre` (cascade create), `__del` (cascade delete), `__cart` (cartesian)
- No cartesian joins between non-utility TOs

**Advantages**: Predictable access paths, isolated changes, self-documenting names. The graph can grow large (500+ TOs) while remaining navigable because the naming convention makes the structure readable without visual inspection.

**Typical profile**:

- TO-to-base-table ratio: 3-8x (each table has several contextual buoys)
- Relationship density: low (0.5-1.0 relationships per TO)
- Graph shape: forest of trees, one tree per anchor

**When encountered**: Solutions designed by developers who prioritize maintainability and team collaboration. Common in ERP, CRM, and complex multi-domain applications. The anchor-buoy pattern was popularized in the FileMaker community through years of reference as a graph management approach.

---

### Primary Topology: Star (Context-Hub)

**Structure**: A small number of high-degree hub TOs (often Globals-based) serve as central dispatchers. Child TOs radiate outward from these hubs. The hubs represent **business contexts** (active company, current billing period, selected product) rather than base table anchors.

**Distinguishing signals**:

- Very few hubs (5-15) but with very high degree (10-40+)
- Hubs are often Globals or session-state TOs, not entity TOs
- Cartesian joins present (Globals cartesian-joined to entity tables for context propagation)
- Naming organized by **context/function** rather than anchor: `Globals_Company`, `Globals_Billing`, `Start_Globals`
- Parallel versioning: V2\_\* TOs coexisting with V1 TOs for gradual migration
- High percentage of degree-0 TOs (leaf access points with no relationships) alongside degree-1 satellites
- Hub-to-TO ratio is very low: 5-10 hubs managing 100+ satellites

**Advantages**: Context changes propagate automatically through the hub. A single global field update (e.g., "active company ID") instantly re-filters all child TOs. Efficient for multi-tenant or multi-context applications.

**Typical profile**:

- TO-to-base-table ratio: 4-15x (many filtered views per entity)
- Relationship density: moderate (0.5-0.7 per TO, but concentrated in hubs)
- Graph shape: small number of large stars, some with secondary sub-hubs

**When encountered**: Production multi-tenant applications, solutions with complex state management. Common in enterprise deployments where context isolation is paramount.

---

### Primary Topology: Tiered Hub

**Structure**: Multiple levels of hubs organized by function. Domain hubs (Events, Games) at the top, function hubs (Edit, Delete, Stats) in the middle, and leaf TOs at the bottom. No single anchor-per-table — instead, the same base table appears under multiple function-specific hubs.

**Distinguishing signals**:

- Multiple super-hubs (degree 15-30+) that are NOT utility/Globals tables — they are core entity TOs
- Function-based naming prefixes: `Edit Games`, `Delete Events`, `Stats Teams`, `LOAD Players`
- Same base table has 20-50+ TOs spread across different functional contexts
- Cartesian joins used for GLOBALS-to-entity connections (session state)
- Clear separation between "domain hubs" (highest degree, entity TOs) and "function hubs" (medium degree, workflow TOs)
- Hub degree distribution is bimodal: a cluster at degree 10-30 (domain) and another at degree 4-10 (function)

**Advantages**: Scales well for applications where the same data is accessed from many different UI workflows. New features add new function-hubs without restructuring domain hubs.

**Typical profile**:

- TO-to-base-table ratio: 8-30x (explosive multiplication of context-specific views)
- Relationship density: moderate (0.7-1.0 per TO)
- Graph shape: multi-level hierarchy with several large clusters

**When encountered**: Complex domain-specific applications (sports management, event coordination) where the same entities participate in many different workflows and each workflow needs its own filtered view.

---

### Primary Topology: Spider-Web

**Structure**: Dense, highly interconnected graph with no clear hierarchy. Many TOs have degree 3+ with cross-connections between different clusters. No dominant naming convention or organizational strategy.

**Distinguishing signals**:

- Low percentage of degree-1 TOs (< 40%)
- No dominant hub TOs — degree distribution is relatively flat
- Many cross-connections between unrelated entities
- Naming is inconsistent or based on ad-hoc descriptions
- Self-joins with non-standard fields (not PK/FK)
- High bridge count (many edges whose removal disconnects the graph)

**Typical profile**:

- TO-to-base-table ratio: 2-4x
- Relationship density: high (1.5+ per TO)
- Graph shape: dense mesh, difficult to decompose into clusters

**When encountered**: Solutions that have grown organically over many years without a deliberate graph management strategy. Also common in smaller solutions where the developer didn't need complex organization because the graph was small enough to manage visually.

**Note**: Spider-web is not inherently "bad" — for solutions with fewer than 30 TOs, the overhead of anchor-buoy or star patterns isn't justified. It becomes a maintenance problem only at scale.

---

### Primary Topology: Flat/Minimal

**Structure**: Few TOs (typically 1-2 per base table), few relationships. Each base table has a single TO. Relationships directly connect these TOs without intermediary views or hubs.

**Distinguishing signals**:

- TO count roughly equals base table count (ratio near 1:1)
- Few or no relationships (data file in a separation model)
- No naming prefix conventions
- All TOs may use `@` prefix or direct base table names

**Typical profile**:

- TO-to-base-table ratio: 1-2x
- Relationship density: very low (< 0.3 per TO)
- Graph shape: scattered nodes with sparse connections, or a simple chain

**When encountered**: Data files in a separation model (the relational structure is expressed through the UI file's external TOs, not within the data file). Also seen in utility files (document storage, logging) and newly created solutions.

---

## Composite Patterns

Real solutions often combine topologies across their file boundaries:

| File Role          | Typical Topology                 | Why                                                                  |
| ------------------ | -------------------------------- | -------------------------------------------------------------------- |
| **UI file**        | Star, Tiered Hub, or Anchor-Buoy | Manages navigation, context, and workflow                            |
| **Data file**      | Flat/Minimal or Anchor-Buoy      | Stores schema; relationships may be minimal if UI file manages joins |
| **Logic file**     | Flat or small Hub-and-Spoke      | Focused on server-side script execution with targeted data access    |
| **Documents file** | Flat                             | Storage only, no complex graph                                       |

A multi-file solution should be classified at both the **per-file** and **solution-wide** level. The solution-wide topology reflects the primary file's management strategy since it orchestrates all cross-file connections.

## Classification Heuristics (for automated detection)

Use these signals to judge the topology from `Reports/profile.md` (graph measures, join operators, layouts by base TO) and the TO names. They are listed in priority order — higher-priority signals can override lower ones.

### Signal: Hub-to-Base-Table Ratio

```
hub_ratio = hub_count / base_table_count
```

- `hub_ratio >= 0.5` and hubs are entity TOs → **Anchor-Buoy** (one hub per table)
- `hub_ratio < 0.15` and max_degree >= 15 → **Star** (few centralized hubs)
- `hub_ratio 0.15-0.5` with bimodal degree distribution → **Tiered Hub**

### Signal: Cartesian Relationship Presence

Cartesian joins (`CartesianProduct` join type) indicate context-propagation patterns:

- Present → likely **Star** or **Tiered Hub** (globals-driven context)
- Absent → likely **Anchor-Buoy** or **Flat**

### Signal: Naming Convention Analysis

| Pattern                                       | Indicates                             |
| --------------------------------------------- | ------------------------------------- |
| `anchor__TABLE__field__modifier`              | Anchor-Buoy                           |
| `Context_Entity` or `Globals_Domain`          | Star (Context-Hub)                    |
| `Function Entity` (Edit, Delete, Stats, LOAD) | Tiered Hub                            |
| `@TableName` only                             | Flat/Minimal (data file)              |
| No consistent pattern                         | Spider-Web or early-stage development |

### Signal: UI Layout Concentration (from button-based classification)

When layout classification is available (see `references/layout-classifications.md`), the **UI-only** layout concentration is a stronger signal than raw layout concentration because it excludes utility, developer, and output layouts that don't reflect navigational patterns.

- `top_to_pct < 0.12` (UI layouts spread across many TOs) → **Anchor-Buoy** (each entity has its own layouts)
- `top_to_pct >= 0.15` (UI layouts dominated by one TO) → **Tiered Hub** (SPA-like, one entity drives the UI)
- Between 0.12-0.15 → weak signal, use other indicators

Layout classification uses `<Button>` element counts from layout XML: layouts with `buttons >= 2` are UI or Output; `buttons <= 1` are Utility/Developer. This separates navigational layouts from implementation-support layouts with 97% accuracy (validated against developer ground truth).

### Signal: Degree Distribution Shape

- **Power-law with long tail**: Many degree-1, few high-degree → Anchor-Buoy or Star
- **Bimodal**: Clusters at degree 1 and degree 10+ → Tiered Hub
- **Flat/uniform**: Most TOs at degree 2-5 → Spider-Web
- **Near-zero**: Most TOs degree 0-1, very few relationships → Flat/Minimal

### Signal: TO-to-Base-Table Ratio

```
to_ratio = total_TOs / base_table_count
```

- `to_ratio < 2` → **Flat/Minimal**
- `to_ratio 2-8` → **Anchor-Buoy** or **Spider-Web**
- `to_ratio 8-15` → **Star** or **Tiered Hub**
- `to_ratio > 15` → **Tiered Hub** (explosive UI-driven multiplication)

### Decision Matrix

| low_degree_pct | max_degree | hub_ratio | cartesian | Classification |
| -------------- | ---------- | --------- | --------- | -------------- |
| >= 0.8         | < 30       | >= 0.5    | No        | Anchor-Buoy    |
| >= 0.7         | >= 15      | < 0.15    | Yes       | Star           |
| >= 0.7         | >= 15      | < 0.15    | No        | Star           |
| >= 0.6         | >= 10      | 0.15-0.5  | Any       | Tiered Hub     |
| < 0.4          | any        | any       | Any       | Spider-Web     |
| any            | < 5        | any       | No        | Flat/Minimal   |
| 0.4-0.7        | any        | any       | Any       | Hybrid         |

## Notes

These classifications are not mutually exclusive — a solution may use anchor-buoy for its data layer and star for its UI layer. The classification describes the **dominant organizational strategy**, not a rigid taxonomy.

The terminology used here (anchor-buoy, spider-web, etc.) reflects common usage within the FileMaker developer community. These patterns have evolved organically through decades of practice as developers found strategies to manage increasingly complex relationship graphs.
