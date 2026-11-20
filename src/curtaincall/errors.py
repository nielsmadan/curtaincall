from collections.abc import Iterable
from typing import Final

_MAX_LISTED: Final = 5


class ReleaseError(Exception):
    pass


class NothingToRelease(Exception):
    pass


class ReleaseCancelled(Exception):
    def __init__(self, message: str, *, left_changes: bool) -> None:
        super().__init__(message)
        self.left_changes = left_changes


def short_id(object_id: str) -> str:
    return object_id[:12]


def name_list(items: Iterable[str]) -> str:
    names = list(items)
    if not names:
        return "none"
    shown = ", ".join(names[:_MAX_LISTED])
    return shown if len(names) <= _MAX_LISTED else f"{shown} and {len(names) - _MAX_LISTED} more"
