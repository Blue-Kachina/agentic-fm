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
