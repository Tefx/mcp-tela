"""Lightweight design-by-contract decorators for Core zone.

Provides @pre and @post decorators that check predicates at runtime.
Predicates are always checked (not debug-only) to ensure contracts
are enforced during testing and production.

These replace the identity-lambda no-ops that were previously defined
per-module.
"""

from __future__ import annotations

import functools
from typing import Any, Callable


def pre(predicate: Callable[..., bool]) -> Callable[[Any], Any]:
    """Precondition decorator: checks predicate against function arguments.

    The predicate lambda must accept the same parameters as the decorated
    function (including defaults).

    Examples:
        >>> @pre(lambda x, y=0: x >= 0)
        ... def calc(x: int, y: int = 0) -> int:
        ...     return x + y
        >>> calc(1)
        1
        >>> calc(-1)
        Traceback (most recent call last):
        ...
        AssertionError: Precondition failed for calc

    Args:
        predicate: Callable that accepts the same args as the decorated function.

    Returns:
        Decorator that wraps the function with precondition checking.
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            assert predicate(*args, **kwargs), f"Precondition failed for {func.__name__}"
            return func(*args, **kwargs)

        return wrapper

    return decorator


def post(predicate: Callable[[Any], bool]) -> Callable[[Any], Any]:
    """Postcondition decorator: checks predicate against return value.

    The predicate lambda receives only the return value.

    Examples:
        >>> @post(lambda result: result >= 0)
        ... def abs_val(x: int) -> int:
        ...     return x if x >= 0 else -x
        >>> abs_val(-5)
        5
        >>> @post(lambda result: result > 0)
        ... def bad() -> int:
        ...     return -1
        >>> bad()
        Traceback (most recent call last):
        ...
        AssertionError: Postcondition failed for bad

    Args:
        predicate: Callable that accepts the return value.

    Returns:
        Decorator that wraps the function with postcondition checking.
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            result = func(*args, **kwargs)
            assert predicate(result), f"Postcondition failed for {func.__name__}"
            return result

        return wrapper

    return decorator
