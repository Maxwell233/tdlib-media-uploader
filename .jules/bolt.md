## 2024-05-15 - Fast Path Check Optimization
**Learning:** `pathlib.Path` instantiation inside tight loops like `os.scandir` during directory traversal is expensive. Filtering by file extension using string manipulation (`os.path.splitext`) on `entry.name` rather than creating a `Path` object up-front provides significant performance gains for filesystem scanning.
**Action:** When filtering files during traversal, prefer native string operations on `entry.name` and defer creating `pathlib.Path` objects until *after* verifying the file passes the filter.
