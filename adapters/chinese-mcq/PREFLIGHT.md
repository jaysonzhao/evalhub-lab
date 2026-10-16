# Chinese MCQ preflight status

The QualBench source is pinned to `mengze-hong/QualBench` commit `ca88e81296ecec80ff7bbde1361c50bf0be2cb21`, file SHA-256 `24ff03a87ef88ef792f52fff832c53099c9780f18227f9abc7821c6e1b80fba9`. The user approved diagnostic exclusions for idx `4224`, `4229`, `4233`, `4234`, `4253`, `4354`, `8131`, `8132`, `8133`, `14861`, and `15175`. The versioned correction manifest records each source type, gold, option inventory, and reason, and verifies the source fields before exclusion.

The resulting score must be labeled **QualBench diagnostic subset** with 17,305 evaluated rows out of 17,316 released rows. The manifest records each excluded ID and reason, original/evaluated domain and type counts, source and canonical hashes, and correction-manifest hash. The three audited type overrides (`1942`, `1945`, and `8515`) remain unchanged.

Dataset preparation is ready to run with:

```bash
python adapters/chinese-mcq/prepare_datasets.py --output-dir prepared-data
```

Before a full run, verify each benchmark manifest entry and canonical JSONL checksum, run a five-row smoke for each benchmark, then validate one item from each observed QualBench domain/type cell. Keep the QualBench diagnostic-subset label on every result and report its denominator.
