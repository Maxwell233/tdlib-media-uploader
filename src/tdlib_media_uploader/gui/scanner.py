"""Connect the media strategy scan to the preview page."""

from __future__ import annotations

from ..config import paths as runtime_paths
from . import config_service
from .config_service import get_config, source_root_for, target_for
from .models import scan_result as preview_result
from .tools import require_kind


def scan_result(kind: str, progress_callback=None, cancel_event=None) -> dict:
    """Scan the configured source and render its Album plans."""
    kind = require_kind(kind)
    config = get_config()
    if config is None:
        raise RuntimeError(config_service._CONFIG_ERROR or "配置不可用。")

    from .integration import scan_v2

    bundle = scan_v2(
        kind,
        source_root=source_root_for(kind),
        target=target_for(kind),
        cancel_event=cancel_event,
        progress_callback=progress_callback,
        config=config,
        runtime_paths=runtime_paths,
    )
    return preview_result(bundle, cancel_event=cancel_event)
