# XML Transformation: Save As XML → fmxmlsnippet

## Overview

`fm_xml_to_snippet.py` converts FileMaker's "Save As XML" export format (a script from the solution's fm-saxml docs, `-s <solution> --script <name>`, or a SaXML script file) into the fmxmlsnippet clipboard format (used in `agent/scripts/` and `agent/sandbox/`). These two formats are structurally distinct and not interchangeable — see `SKILL.md` in `.cursor/skills/script-review/` for the full breakdown.

---

## Architecture

The converter is **catalog-driven**: it re-declares no per-step structure.

1. `saxml_read.read_saxml_step` decodes a SaXML `<Step>`'s nested `<ParameterValues>` into the shared step instance, guided entirely by the catalog `params[]` grammar.
2. `catalog_emit.convert_step_with_catalog` emits that instance as fmxmlsnippet, again from the catalog grammar (a real-id resolver preserves the step's own field/script/layout ids).

The only hand-coded steps are the sanctioned control-flow set: `# (comment)`, If/Else If/Else/End If, Loop/Exit Loop If/End Loop, Exit Script, Set Variable. Everything else — including advanced facets like `attrGroup`, `bitmaskGroup`, `fieldList`, `findRequests`, `parametersList`, and the discriminator families — is decoded generically. Coverage is complete: 0 uncatalogued and 0 undecodable steps across the full script corpus.

## Verification

- **Offline gate (any contributor):** `uvx pytest agent/scripts/test_converter_conformance.py` compares the converter's output against the golden fixtures in `agent/fixtures/converter/` (`saxml/*.xml` → `saxml-to-snippet.json`, and the fmxmlsnippet → HR corpus). See `agent/docs/CONVERTERS.md` § Conformance.
- **Adding or fixing a decoder:** extend the *grammar* generically (never a per-step case). If a SaXML shape can't be expressed by the existing catalog facets, author a new generic facet in the catalog rather than special-casing the step — then re-run the gate.
- **Maintainer live check:** the authoritative oracle is a live FileMaker round-trip; the golden fixtures cache its verdict so the offline gate stays meaningful without it.

## Appendix — SaXML → fmxmlsnippet structural mappings

A reference for reading the decoder: representative shapes `saxml_read` maps from the SaXML `<ParameterValues>` tree to fmxmlsnippet elements.

| xml_parsed pattern                                 | fmxmlsnippet pattern                                                 | Notes                |
| -------------------------------------------------- | -------------------------------------------------------------------- | -------------------- |
| `Boolean type="With dialog" value="False"`         | `<NoInteract state="True"/>`                                         | Inverted             |
| `Boolean type="Select" value="True"`               | `<SelectAll state="True"/>`                                          | Direct               |
| `Boolean type="Collapsed"`                         | `<Restore state="..."/>`                                             | Direct               |
| `Boolean type="Verify SSL Certificates"`           | `<VerifySSLCertificates state="..."/>`                               | Direct               |
| `Boolean type="In external browser"`               | `<Option state="..."/>` (Open URL)                                   | Direct               |
| `Boolean type="Skip auto-enter options"`           | `PerformAutoEnter="..."`                                             | Direct (same value)  |
| `Options ShowRelated="True"` (GTRR)                | `<Option state="False"/>`                                            | Inverted             |
| `URL autoEncode="True"`                            | `<DontEncodeURL state="False"/>`                                     | Inverted             |
| `Parameter type="Target" > Variable value="$x"`    | `<Field>$x</Field>`                                                  | Variable target      |
| `Parameter type="Target" > FieldReference`         | `<Field table="" id="" name=""/>`                                    | Field target         |
| `LayoutReferenceContainer Label="original layout"` | `<LayoutDestination value="CurrentLayout"/>` (no `<Layout>` element) |                      |
| `Animation name="Cross Dissolve"`                  | `<Animation value="CrossDissolve"/>`                                 | Strip spaces         |
| `Location` CDATA (Get File Exists)                 | `<UniversalPathList>path</UniversalPathList>`                        | Raw text, not nested |
| `Text value="..."` (Insert Text)                   | `<Text>...</Text>` with `\r` → `&#xD;`                               | CR entity encoding   |

---
