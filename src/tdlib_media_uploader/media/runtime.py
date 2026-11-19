"""Task-local inputs for the legacy media helpers and their worker pools."""

from contextlib import contextmanager
from contextvars import ContextVar

from ..config.snapshot import snapshot_config
from ..core.filesystem import stable_path

_CONFIG = ContextVar("media_config", default=None)
_DATA = ContextVar("media_data", default=None)

_SCOPE_VALUES = {
    "LAST_SCAN_ERRORS": list,
    "LAST_SCAN_WARNINGS": list,
    "LAST_SCAN_SIZE_SKIPS": list,
    "LAST_SCAN_PREMIUM_REQUIRED": list,
    "LAST_SCAN_IGNORED_ROOT_MEDIA": list,
    "LAST_SCAN_SNAPSHOTS": dict,
    "IMAGE_SCAN_SNAPSHOTS": dict,
    "STAGED_UPLOAD_PATHS": dict,
}


def pipeline_config(default):
    config = _CONFIG.get()
    return default if config is None else config


def pipeline_locals(namespace):
    data = _DATA.get()
    return namespace if data is None else data[namespace["__name__"]]


def pipeline_value(module, name, default=None):
    data = _DATA.get()
    if data is not None:
        values = data.get(getattr(module, "__name__", ""))
        if values is not None and name in values:
            return values[name]
    return getattr(module, name, default)


@contextmanager
def media_scope(config, *, kind=None, root=None, items=()):
    parent = _DATA.get()
    current = _CONFIG.get()
    if root is not None and getattr(config, f"{kind.upper()}_DIR", None) != root:
        config = snapshot_config(config, immutable=True, overrides={f"{kind.upper()}_DIR": root})
    if parent is not None and current is config and not items:
        yield
        return

    data = {}
    for mode in ("video", "image", "mixed"):
        name = f"{__package__}.{mode}_pipeline"
        values = {key: factory() for key, factory in _SCOPE_VALUES.items()}
        if parent is not None:
            values["STAGED_UPLOAD_PATHS"] = parent[name]["STAGED_UPLOAD_PATHS"]
        data[name] = values
    for item in items:
        name = f"{__package__}.{item.media_kind}_pipeline"
        key = "IMAGE_SCAN_SNAPSHOTS" if item.media_kind == "image" else "LAST_SCAN_SNAPSHOTS"
        data[name][key][stable_path(item.path)] = item.snapshot.as_tuple()

    config_token = _CONFIG.set(config)
    data_token = _DATA.set(data)
    try:
        yield
    finally:
        _DATA.reset(data_token)
        _CONFIG.reset(config_token)
