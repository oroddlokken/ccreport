"""Assertions that narrow a type as a side effect of checking it.

The cache readers answer `dict | None` because a missing row is a real answer.
A test that just wrote the row knows it is there, and says so once with these
instead of asserting it separately at every use.
"""

from __future__ import annotations


def present[T](value: T | None) -> T:
    """Assert *value* is not None and return it."""
    assert value is not None
    return value
