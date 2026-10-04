"""Immutable file-stat checks shared by preparation and transport."""
from pathlib import Path


class SourceChanged(OSError):
    """Content changed before submission and requires a fresh scan."""
    submitted = False


def capture_snapshot(path):
    path = Path(path)
    stat = path.stat()
    return path, stat.st_size, stat.st_mtime_ns


def validate_snapshots(snapshots):
    for path, size, mtime in snapshots:
        try:
            stat = path.stat()
            if not path.is_file() or (stat.st_size, stat.st_mtime_ns) != (size, mtime):
                raise OSError("文件已变化")
        except OSError as exc:
            raise SourceChanged(f"准备期间文件变化或不可读取，请重新扫描：{path}") from exc
