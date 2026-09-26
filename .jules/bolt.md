## 2024-05-24 - Fast Tuple Sorting Replaces cmp_to_key
**Learning:** In the `sorting.py` logic, utilizing `functools.cmp_to_key` with a custom comparison function for $O(N \log N)$ `natural_compare` calls severely degraded performance on large lists of files. However, Python's built-in tuple sorting naturally implements identical fallback logic when structured correctly.
**Action:** When implementing custom sorting logic, transform the data into a hierarchical, structurally comparable tuple that Python can sort natively in C, avoiding `cmp_to_key` wherever possible.
## 2024-05-24 - File System Error Classification Optimization
**Learning:** Python creates sets/tuples in function bodies dynamically by default if they contain variables/function calls (like `getattr(errno, ...)`), resulting in redundant allocations per function call. In a file scanner, this adds up quickly.
**Action:** Extract these to module-level constants to avoid redundant allocations on tight filesystem iterations.
