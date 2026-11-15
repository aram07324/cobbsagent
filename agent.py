"""Compatibility wrapper for the modular dependency-security agent."""

from __future__ import annotations

import sys
import types
import urllib.error
import urllib.parse
import urllib.request

from cobbs import *
from cobbs import __all__
import cobbs.agent_loop as _agent_loop
import cobbs.safety as _safety
from cobbs.sandbox import run_tests as m1_run_tests

class _AgentCompatibilityModule(types.ModuleType):
    """Forward legacy monkeypatches to the implementation modules."""

    def __setattr__(self, name: str, value: object) -> None:
        super().__setattr__(name, value)
        if name == "MAX_STEPS":
            _agent_loop.MAX_STEPS = value
        elif name == "m1_run_tests":
            _safety.m1_run_tests = value

sys.modules[__name__].__class__ = _AgentCompatibilityModule
