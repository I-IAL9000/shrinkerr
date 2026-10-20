"""An error as a log line names it (v0.10.0)."""


def describe_error(exc: BaseException) -> str:
    """Its type, and its message when it has one — a timeout's is empty, which
    left lines like "Couldn't read radarr tags for rules:" with nothing after."""
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__
