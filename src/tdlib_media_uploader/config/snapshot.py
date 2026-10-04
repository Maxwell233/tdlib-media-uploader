"""Per-task configuration snapshots; editing settings cannot retarget a send."""
from copy import deepcopy
from types import SimpleNamespace, MappingProxyType
from collections.abc import Mapping

from ..core.identity import canonical_target


def _copy(value):
    if isinstance(value, Mapping):
        return {key: _copy(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return type(value)(_copy(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return type(value)(_copy(item) for item in value)
    return deepcopy(value)


def _freeze(value):
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (tuple, list)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze(item) for item in value)
    return value


class FrozenConfig(SimpleNamespace):
    """Read-only task settings, including nested collections."""
    def __init__(self, **values):
        super().__init__(**{key: _freeze(value) for key, value in values.items()})

    def __setattr__(self, name, value):
        raise AttributeError("运行中的任务配置不可修改")

    def __delattr__(self, name):
        raise AttributeError("运行中的任务配置不可修改")


def snapshot_config(config, kind=None, target=None, *, immutable=False, overrides=None):
    values = {name: _copy(getattr(config, name)) for name in dir(config)
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
    if overrides:
        vars(result).update(overrides)
    return FrozenConfig(**vars(result)) if immutable else result
