"""Snapshot contract between media preparation and asynchronous transport."""
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Mapping

from ..core.source_snapshot import SourceChanged, validate_snapshots


@dataclass(frozen=True, slots=True)
class PreparedMedia:
    sources: tuple[tuple[Path, int, int], ...]
    artifacts: tuple[tuple[Path, int, int], ...]

    def validate(self):
        validate_snapshots((*self.sources, *self.artifacts))


def _local_paths(value):
    if isinstance(value, Mapping):
        if value.get("@type") == "inputFileLocal":
            yield Path(value["path"])
        for child in value.values():
            yield from _local_paths(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _local_paths(child)


def capture_prepared_media(items, contents):
    sources = tuple((item.path, item.snapshot.size, item.snapshot.mtime_ns) for item in items)
    PreparedMedia(sources, ()).validate()
    artifacts = []
    for path in dict.fromkeys(_local_paths(contents)):
        try:
            stat = path.stat()
        except OSError as exc:
            raise SourceChanged(f"准备后的文件不可读取，请重新扫描：{path}") from exc
        artifacts.append((path, stat.st_size, stat.st_mtime_ns))
    result = PreparedMedia(sources, tuple(artifacts))
    result.validate()
    return result
