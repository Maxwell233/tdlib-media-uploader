"""Signature adaptation shared by legacy boundaries."""
import inspect
from typing import Any, Callable, Sequence

def call_supported(function: Callable[..., Any], args: Sequence[Any] = (), **kwargs: Any) -> Any:
    """Call a collaborator while allowing small fakes with fewer keywords."""

    try:
        signature = inspect.signature(function)
    except (TypeError, ValueError):
        return function(*args, **kwargs)

    parameters = signature.parameters
    positional = list(args)
    accepted: dict[str, Any] = {}
    var_keyword = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    for name, value in kwargs.items():
        parameter = parameters.get(name)
        if parameter is not None and parameter.kind is inspect.Parameter.POSITIONAL_ONLY:
            positional.append(value)
        elif parameter is not None or var_keyword:
            accepted[name] = value
    return function(*positional, **accepted)
