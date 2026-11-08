# Public snapshot: privacy review

This repository is published as a fresh root commit, not as a continuation of its private development history.

## Scope and changes

- Reviewed the complete source snapshot, documentation, evaluation corpus and query labels.
- Removed machine-specific storage locations, hardware inventory, personal maintenance schedules, personal operator workflow and unrelated agent configuration preferences.
- Replaced deployment-specific examples with relative paths or explicit placeholders.
- Retained database architecture, configuration options, common local ports, project requirements, technical decisions and general synthetic evaluation topics.
- Removed references to private development commit/task identifiers. Historical project reports remain historical and do not imply current acceptance.
- Reviewed credential-pattern matches in context. Matches were synthetic test data and detector code; no real credentials were identified in the inspected snapshot. This is not an absolute guarantee or a commercial secret-scanner certification.
- Application code, tests, scripts, resource dictionaries and package configuration were not changed by privacy remediation.

## Checks performed

Validation used an existing Python 3.11.15 virtual environment with pytest, sqlite-vec and the application dependencies. The optional jieba package was absent; retrieval figures below therefore use the available fallback tokenizer and must not be compared directly with historical jieba results.

- `python -m pytest tests/test_eval.py -o addopts= -q -ra`: 4 passed, 0 skipped.
- `python -m compileall -q src tests scripts`: exit 0.
- Evaluation corpus: 32 documents, 44 queries; all expected document identifiers resolve.
- FTS5 evaluation through `eval.run_eval` functions: Recall@1 = 0.568182, Recall@5 = 0.954545, Recall@10 = 1.000000, MRR@10 = 0.756629, NDCG@10 = 0.814379.
- The FTS5 CLI calculated metrics but its TemporaryDirectory cleanup raised Windows file-lock error 32. A separate subprocess calling the same evaluation functions with an explicit temporary database completed successfully. The CLI cleanup issue was not fixed as part of privacy remediation.
- A fresh-root `git diff --cached --check` reports two pre-existing blank-at-EOF findings in `tests/test_doctor.py` and `tests/test_query_rewrite.py`; those files were preserved byte-for-byte. Documentation remediation introduced no new whitespace findings.
- No full application acceptance suite or production provider/deployment validation was performed for this documentation-only publication.

## Limitations

A new root removes old commits from advertised branch history; it does not itself purge hosting-provider caches, dangling Git objects, existing clones or external copies. Publication status and server-side history reachability must be checked separately. Deployment paths and credentials must be supplied locally and never committed.
