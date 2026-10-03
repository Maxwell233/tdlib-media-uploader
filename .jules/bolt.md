## 2024-05-24 - Fast Tuple Sorting Replaces cmp_to_key
**Learning:** In the `sorting.py` logic, utilizing `functools.cmp_to_key` with a custom comparison function for $O(N \log N)$ `natural_compare` calls severely degraded performance on large lists of files. However, Python's built-in tuple sorting naturally implements identical fallback logic when structured correctly.
**Action:** When implementing custom sorting logic, transform the data into a hierarchical, structurally comparable tuple that Python can sort natively in C, avoiding `cmp_to_key` wherever possible.
## 2024-05-24 - Legacy Sorting
**Learning:** Replaced the legacy `functools.cmp_to_key` implementation in `filesystem_legacy.py` with native tuples (Schwartzian transform). Learned that ensuring the precise standard sorting order requires careful attention to the data types stored in the key tuples (e.g. keeping digits at type `1` vs letters at type `0` so numbers correctly sort *after* letters).
**Action:** When migrating from `cmp_to_key` to tuple-based sort keys, ensure the structural type values generated match the legacy behavior.
