"""ContextManager Utilities

This module implements helpers around python context-managers, with-statements,
and RAII. It is meant as a supplement to `contextlib` from the python standard
library.
"""

import contextlib


__all__ = [
    "suppress_oserror",
]


@contextlib.contextmanager
def suppress_oserror(*errnos):
    """Suppress OSError Exceptions

    This is an extension to `contextlib.suppress()` from the python standard
    library. It catches any `OSError` exceptions and suppresses them. However,
    it only catches the exceptions that match the specified error numbers.

    Parameters
    ----------
    errnos
        A list of error numbers to match on. If none are specified, this
        function has no effect.
    """

    try:
        yield
    except OSError as e:
        if e.errno not in errnos:
            raise e


class GuardedCM(contextlib.AbstractContextManager):
    """Guarded Context Manager

    This class implements a context-manager that calls `__exit__()` even if
    entering the context raised an exception. Furthermore, it provides an
    `ExitStack` as `self.exit_stack` and thus allows opening further contexts
    and stacking them.

    To use this context-manager, simply override the `guarded_enter()` and
    `guarded_exit()` methods. You do not have to call the super-methods from
    your functions (since they merely provide stubs).
    """

    def guarded_enter(self):
        return self

    def guarded_exit(self):
        pass

    @property
    def guarded_stack(self):
        return self._guarded_exit_stack

    def __enter__(self):
        self._guarded_exit_stack = contextlib.ExitStack()
        try:
            return self.guarded_enter()
        except:
            with self._guarded_exit_stack:
                self.guarded_exit()
            raise

    def __exit__(self, exc_type, exc_value, exc_tb):
        with self._guarded_exit_stack:
            self.guarded_exit()
