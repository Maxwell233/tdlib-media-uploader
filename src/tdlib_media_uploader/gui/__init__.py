"""GUI boundary, application window and Qt-free V2 integration adapters."""


def gui_dependency_errors():
    """Check packaged Qt modules without constructing a window."""

    from importlib import import_module

    failures = []
    for module in ("PySide6", "PySide6.QtSvg"):
        try:
            import_module(module)
        except Exception as error:
            failures.append(f"导入 {module} 失败：{type(error).__name__}: {error}")
    return failures
