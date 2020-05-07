#
# Tests for the 'osbuild.util.ctx' module.
#

import errno
import unittest

from osbuild.util import ctx


class TestUtilCtx(unittest.TestCase):
    def test_suppress_oserror(self):
        #
        # Verify the `suppress_oserror()` function.
        #

        # Empty list and empty statement is a no-op.
        with ctx.suppress_oserror():
            pass

        # Single errno matches raised errno.
        with ctx.suppress_oserror(errno.EPERM):
            raise OSError(errno.EPERM, "Operation not permitted")

        # Many errnos match raised errno regardless of their order.
        with ctx.suppress_oserror(errno.EPERM, errno.ENOENT, errno.ESRCH):
            raise OSError(errno.EPERM, "Operation not permitted")
        with ctx.suppress_oserror(errno.ENOENT, errno.EPERM, errno.ESRCH):
            raise OSError(errno.EPERM, "Operation not permitted")
        with ctx.suppress_oserror(errno.ENOENT, errno.ESRCH, errno.EPERM):
            raise OSError(errno.EPERM, "Operation not permitted")

        # Empty list re-raises exceptions.
        with self.assertRaises(OSError):
            with ctx.suppress_oserror():
                raise OSError(errno.EPERM, "Operation not permitted")

        # Non-matching lists re-raise exceptions.
        with self.assertRaises(OSError):
            with ctx.suppress_oserror(errno.ENOENT):
                raise OSError(errno.EPERM, "Operation not permitted")
            with ctx.suppress_oserror(errno.ENOENT, errno.ESRCH):
                raise OSError(errno.EPERM, "Operation not permitted")

    def test_guarded_cm(self):
        #
        # Test the `GuardedCM` context-manager utility.
        #

        # A global state object that allows controlling the context manager.
        state = {
            "counter": 0,
            "enter": lambda _: None,
            "exit": lambda _: None,
        }

        # A simple context-manager that increases the counter when entering a
        # context, and decreases it when exiting a context. Furthermore, it
        # calls the enter/exit callbacks from the state object.
        class TestCM(ctx.GuardedCM):
            _state = state

            def guarded_enter(self):
                self._state["counter"] += 1
                return self._state["enter"](self)

            def guarded_exit(self):
                self._state["counter"] -= 1
                return self._state["exit"](self)

        # Verify entering the context calls the correct overriden methods.
        state["enter"] = lambda _: None
        state["exit"] = lambda _: None
        assert state["counter"] == 0
        with TestCM() as v:
            assert v is None
            assert state["counter"] == 1
        assert state["counter"] == 0

        # Verify raising an exception from within the context works as normal.
        state["enter"] = lambda _: None
        state["exit"] = lambda _: None
        with self.assertRaises(SystemError):
            assert state["counter"] == 0
            with TestCM():
                assert state["counter"] == 1
                raise SystemError
        assert state["counter"] == 0

        # Verify `guarded_exit()` is called even when `guarded_enter()` raises
        # an exception.
        def enter_inc_raise(_self):
            assert state["counter"] == 1
            raise SystemError
        state["enter"] = enter_inc_raise
        state["exit"] = lambda _: None
        with self.assertRaises(SystemError):
            assert state["counter"] == 0
            with TestCM():
                assert False
        assert state["counter"] == 0

        # Verify the return-value is taken from `guarded_enter()`, and
        # returning from `guarded_exit()` has no effect.
        state["enter"] = lambda _: 71
        state["exit"] = lambda _: True
        assert state["counter"] == 0
        with TestCM() as v:
            assert v == 71
            assert state["counter"] == 1
        assert state["counter"] == 0

        # Verify the exit-stack is correctly cleaned up.
        def cb_dec():
            state["counter"] -= 1
        def enter_stacked(self):
            state["counter"] += 2
            self.guarded_stack.callback(cb_dec)
            self.guarded_stack.callback(cb_dec)
        state["enter"] = enter_stacked
        state["exit"] = lambda _: None
        assert state["counter"] == 0
        with TestCM():
            assert state["counter"] == 3
        assert state["counter"] == 0

        # Verify the exit-stack is correctly cleaned up when `guarded_enter()`
        # raises an exception.
        def enter_stacked_raise(self):
            state["counter"] += 2
            self.guarded_stack.callback(cb_dec)
            self.guarded_stack.callback(cb_dec)
            assert state["counter"] == 3
            raise SystemError
        state["enter"] = enter_stacked_raise
        state["exit"] = lambda _: None
        with self.assertRaises(SystemError):
            assert state["counter"] == 0
            with TestCM():
                assert False
        assert state["counter"] == 0
