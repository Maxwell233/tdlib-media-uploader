## 2024-05-24 - Fast Tuple Sorting Replaces cmp_to_key
**Learning:** In the `sorting.py` logic, utilizing `functools.cmp_to_key` with a custom comparison function for $O(N \log N)$ `natural_compare` calls severely degraded performance on large lists of files. However, Python's built-in tuple sorting naturally implements identical fallback logic when structured correctly.
**Action:** When implementing custom sorting logic, transform the data into a hierarchical, structurally comparable tuple that Python can sort natively in C, avoiding `cmp_to_key` wherever possible.
## 2024-05-24 - Legacy Sorting
**Learning:** Replaced the legacy `functools.cmp_to_key` implementation in `filesystem_legacy.py` with native tuples (Schwartzian transform). Learned that ensuring the precise standard sorting order requires careful attention to the data types stored in the key tuples (e.g. keeping digits at type `1` vs letters at type `0` so numbers correctly sort *after* letters).
**Action:** When migrating from `cmp_to_key` to tuple-based sort keys, ensure the structural type values generated match the legacy behavior.
## 2026-10-12 - Cached cancellable video metadata reading
**Learning:** functools.lru_cache cannot be effectively used on functions that accept a `cancel_event`, because passing the event makes every call unique or caches a cancelled/transient state. This forces GUI rescans (which must pass cancel_event) to repeatedly spawn FFmpeg subprocesses to parse unchanged video metadata, a significant bottleneck.
**Action:** Replaced lru_cache with a custom bounded dictionary cache keyed by (path, size, mtime) that handles cancel_event explicitly by avoiding caching cancelled results and safely locking access across scan threads.
