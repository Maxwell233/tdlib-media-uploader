
## 2024-05-18 - pathlib.Path instantiation overhead in tight loops
**Learning:** Instantiating `pathlib.Path` objects inside tight loops, such as directory scanning (checking extensions) and file modification time lookups (`file_mtime`), can introduce significant overhead. For example, `Path(path).stat().st_mtime` takes roughly ~4x as long as `os.stat(path).st_mtime` for 100k calls, and `os.path.splitext` is about ~2x faster than `Path(entry.path).suffix` inside `os.scandir` loops.
**Action:** Always favor using `os.stat()` over `Path().stat()` when the `Path` object is only instantiated temporarily to call `.stat()`. Similarly, use string manipulation like `os.path.splitext()` instead of `Path().suffix` when processing large numbers of strings or `os.scandir` entries.
