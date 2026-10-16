# Chinese MCQ preflight status

The adapter's pinned QualBench release is not safe to package yet. `prepare_datasets.py` validates every answer against the choices present in the released record and currently stops at `qualbench/4224`: the released gold is `ABD`, but the question contains only choices `A`, `B`, and `C`. The source file is pinned to commit `ca88e81296ecec80ff7bbde1361c50bf0be2cb21` and SHA256 `24ff03a87ef88ef792f52fff832c53099c9780f18227f9abc7821c6e1b80fba9`.

The correction manifest contains only the three type-label corrections specified by the migration procedure. It does not guess a missing option or change this row's gold. The source dataset is not copied into this repository. Re-run preparation only after an authoritative source or approved correction resolves the inconsistency; then complete the four-benchmark smoke evaluation before a full run.
