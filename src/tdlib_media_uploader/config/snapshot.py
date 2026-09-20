"""Per-task configuration snapshots; editing settings cannot retarget a send."""
from copy import deepcopy
from types import SimpleNamespace

from ..core.identity import canonical_target


def snapshot_config(config, kind=None, target=None):
    values = {name: deepcopy(getattr(config, name)) for name in dir(config)
              if name.isupper() and not name.startswith('_')}
    targets = {}
    provider = getattr(config, 'target_for', None)
    if callable(provider):
        targets = {name: dict(provider(name)) for name in ('video', 'image', 'mixed')}
    if kind and target is not None:
        targets[kind] = dict(target)
    result = SimpleNamespace(**values)
    result.target_for = lambda name: dict(targets.get(name, {}))
    selected = canonical_target(target or {})
    if selected:
        for name, value in selected.items():
            setattr(result, name.upper(), value)
        result.GROUP_CHAT_ID = selected['chat_id']
    return result
