"""Tests for the back-compat shims over the ProcessHost seam.

The kill/liveness bodies (and their pid<=0 guards) now live in
``automator.process_host`` — see ``test_process_host.py``. These cover only that
the legacy ``platform_util`` entry points still delegate, plus the real
``detach_kwargs`` that stayed behind."""

from __future__ import annotations

import os
import sys

import pytest

from automator import platform_util


def test_pid_alive_shim_true_for_self():
    assert platform_util.pid_alive(os.getpid()) is True


def test_pid_alive_shim_false_for_non_positive():
    assert platform_util.pid_alive(0) is False
    assert platform_util.pid_alive(-1) is False


def test_terminate_pid_shim_noop_for_non_positive():
    # delegates to the host, whose pid<=0 guard short-circuits before any signal
    platform_util.terminate_pid(0)  # no raise, no signal
    platform_util.terminate_pid(-42)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX detach branch")
def test_detach_kwargs_posix():
    assert platform_util.detach_kwargs() == {"start_new_session": True}
