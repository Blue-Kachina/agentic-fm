# fm-saxml test fixtures

Save a Copy as XML exports that tests convert with fm-saxml at run time, so they always
test against the installed fm-saxml version. Used by `agent/scripts/test_solution_context.py`.

## Bagel/

`EverythingBagel.xml` and `Empty.xml`: a two-file solution saved with FileMaker Pro 26.0.3
(SaXML 2.3.0.0). EverythingBagel has a data source pointing at Empty, so converting the
folder gives a multi-file build; converting `EverythingBagel.xml` alone gives one file's docs.

Copied unchanged from `samples/by_type/second_file/` in fm-saxml-converter
(<https://github.com/Blue-Kachina/fm-doc-saxml>, commit 076d510, MIT licence, copyright (c) 2026
Matthew Leering). `.gitattributes` keeps the bytes as exported.

## Multi/

`FileA.xml` and `FileB.xml`: a synthetic two-file solution where FileB has a relationship and a
layout that use FileA's fields through an external table occurrence. Converting the folder gives
a multi-file build whose FileA docs list those uses in `backlinks.json`. Used by
`agent/scripts/test_trace_query_dead.py` (uses from other files).

Copied unchanged from `samples/multi_file_export/SaXML_no_separate_catalogue/` in
fm-saxml-converter (commit 780df2f, same licence as above). Its `Summary.xml` is left out: it
holds the local paths of the machine that exported it, and the build doesn't need it.
