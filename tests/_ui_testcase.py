"""Opt-in unittest bases for fixtures which exercise private UI internals.

Public caller/REPL/worker tests intentionally continue using ordinary unittest
bases. These helpers send only opted-in fixtures to the real production owner;
they never relax ownership checks or replace the runtime implementation.
"""

import asyncio
from contextvars import copy_context
from functools import wraps
import inspect
import unittest

from pysual._engine import call, call_async


def ui_timeout(seconds):
    """Give an unusually large owner test its own bounded execution budget."""
    def decorate(method):
        method._ui_timeout = seconds
        return method
    return decorate


class _OwnerClassFixtures:
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        for name in ("setUpClass", "tearDownClass"):
            fixture = vars(cls).get(name)
            if isinstance(fixture, classmethod):
                original = fixture.__func__

                @wraps(original)
                def routed(owner, _fixture=original):
                    return call(_fixture, owner)

                setattr(cls, name, classmethod(routed))


class UIOwnerTestCase(_OwnerClassFixtures, unittest.TestCase):
    def _callSetUp(self):
        call(self.setUp)

    def _callTestMethod(self, method):
        invoke = super()._callTestMethod
        timeout = getattr(method, "_ui_timeout", None)
        if timeout is None:
            return call(invoke, method)

        async def run():
            await asyncio.wait_for(call_async(invoke, method), timeout)

        # The caller's loop owns the deadline; the production engine's shared
        # dispatch timeout stays unchanged, including during this test.
        asyncio.run(run())

    def _callTearDown(self):
        call(self.tearDown)

    def _callCleanup(self, function, /, *args, **kwargs):
        call(function, *args, **kwargs)


class AsyncUIOwnerTestCase(_OwnerClassFixtures, unittest.IsolatedAsyncioTestCase):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._ui_test_context = copy_context()

    async def _invoke_on_owner(self, function, args, kwargs):
        result = self._ui_test_context.run(function, *args, **kwargs)
        if inspect.isawaitable(result):
            # Keep fixture ContextVars across the separate setUp/test/tearDown
            # calls, while all their asyncio tasks belong to the owner loop.
            return await asyncio.create_task(result, context=self._ui_test_context)
        return result

    def _callSetUp(self):
        self._asyncioRunner.get_loop()
        call(self._ui_test_context.run, self.setUp)
        self._callAsync(self.asyncSetUp)

    def _callAsync(self, function, /, *args, **kwargs):
        return self._asyncioRunner.run(
            call_async(self._invoke_on_owner, function, args, kwargs),
            context=self._asyncioTestContext,
        )

    def _callMaybeAsync(self, function, /, *args, **kwargs):
        if inspect.iscoroutinefunction(function):
            return self._callAsync(function, *args, **kwargs)
        return call(self._ui_test_context.run, function, *args, **kwargs)

    def _callTearDown(self):
        self._callAsync(self.asyncTearDown)
        call(self._ui_test_context.run, self.tearDown)
