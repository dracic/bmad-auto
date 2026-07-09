"""Tests for the back-compat shims over the ProcessHost seam.

The kill/liveness bodies (and their pid<=0 guards) now live in
``bmad_loop.process_host`` — see ``test_process_host.py``. These cover only that
the legacy ``platform_util`` entry points still delegate, plus the real
``detach_kwargs`` that stayed behind."""

from __future__ import annotations

import os
import sys

import pytest

from bmad_loop import platform_util


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


@pytest.mark.parametrize(
    "value",
    [
        "/etc/passwd",  # POSIX-absolute — rejected even when running on Windows
        "C:\\Windows\\system32",  # Windows-absolute — rejected even on POSIX
        "C:/Windows",
        "\\\\server\\share",  # UNC root
        "C:foo",  # Windows drive-*relative* — still drive-qualified, intentionally rejected
    ],
)
def test_is_absolute_path_rejects_both_flavors(value):
    assert platform_util.is_absolute_path(value) is True


@pytest.mark.parametrize("value", [".claude/skills", "a/b/c.json", "file.txt", "."])
def test_is_absolute_path_accepts_relative(value):
    assert platform_util.is_absolute_path(value) is False


@pytest.mark.parametrize(
    "value",
    ["../etc", "../../secrets", "a/../../b", "a\\..\\b", "..", "nested/dir/../x"],
)
def test_has_parent_ref_detects_escapes(value):
    assert platform_util.has_parent_ref(value) is True


@pytest.mark.parametrize("value", [".claude/skills", "a/b/c", "..hidden", "a..b/c"])
def test_has_parent_ref_ignores_non_segments(value):
    # `..hidden` / `a..b` contain the substring but not a `..` path segment.
    assert platform_util.has_parent_ref(value) is False


# ---------------------------------------------------------------- atomic_replace


def _flaky_replace(fail_times: int, real=os.replace):
    """os.replace that raises a sharing violation the first ``fail_times`` calls."""
    calls = {"n": 0}

    def replace(src, dst):
        calls["n"] += 1
        if calls["n"] <= fail_times:
            raise PermissionError(5, "Access is denied")
        real(src, dst)

    return replace, calls


def test_atomic_replace_retries_then_succeeds(tmp_path, monkeypatch):
    monkeypatch.setattr(platform_util.sys, "platform", "win32")
    sleeps: list[float] = []
    monkeypatch.setattr(platform_util.time, "sleep", lambda s: sleeps.append(s))

    replace, calls = _flaky_replace(2)
    monkeypatch.setattr(platform_util.os, "replace", replace)

    src = tmp_path / "s.tmp"
    src.write_text("x", encoding="utf-8")
    dst = tmp_path / "d.json"
    platform_util.atomic_replace(src, dst)

    assert calls["n"] == 3
    assert len(sleeps) == 2  # one backoff before each retry
    assert dst.read_text(encoding="utf-8") == "x"


def test_atomic_replace_permanent_failure_reraises(tmp_path, monkeypatch):
    monkeypatch.setattr(platform_util.sys, "platform", "win32")
    monkeypatch.setattr(platform_util.time, "sleep", lambda _s: None)

    def always_denied(src, dst):
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(platform_util.os, "replace", always_denied)

    with pytest.raises(PermissionError):
        platform_util.atomic_replace(tmp_path / "s", tmp_path / "d")


def test_atomic_replace_no_retry_on_posix(tmp_path, monkeypatch):
    monkeypatch.setattr(platform_util.sys, "platform", "linux")
    sleeps: list[float] = []
    monkeypatch.setattr(platform_util.time, "sleep", lambda s: sleeps.append(s))

    def denied(src, dst):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(platform_util.os, "replace", denied)

    with pytest.raises(PermissionError):
        platform_util.atomic_replace(tmp_path / "s", tmp_path / "d")
    assert sleeps == []  # zero backoff — a real POSIX error surfaces at once


# ------------------------------------------------------------------ safe_segment


def _is_legal_segment(seg: str) -> bool:
    return (
        bool(seg)
        and len(seg) <= platform_util._MAX_SEGMENT
        and not platform_util._ILLEGAL_SEGMENT_CHARS.search(seg)
        and not seg.endswith((" ", "."))
        and not platform_util._is_reserved_basename(seg)
    )


@pytest.mark.parametrize(
    "value", ["3-2-digest-delivery", "epic1_story2", "a.b.c", "plain", "console"]
)
def test_safe_segment_identity_for_clean_input(value):
    # a legal segment (incl. the non-reserved 'console') is returned byte-identical
    assert platform_util.safe_segment(value) == value


@pytest.mark.parametrize(
    "value, base",
    [
        ('a<b>c:"d/e\\f|g?h*i', "a_b_c__d_e_f_g_h_i"),  # every illegal char -> _ (`:"` = two)
        ("with\ttab", "with_tab"),  # control char
        ("x.", "x"),  # trailing dot stripped
        ("y ", "y"),  # trailing space stripped
        ("CON", "_CON"),  # reserved basename
        ("nul", "_nul"),  # case-insensitive
        ("COM1.txt", "_COM1.txt"),  # reserved even with extension
        ("LPT9", "_LPT9"),
        ("COM0", "_COM0"),  # COM0/LPT0 are reserved too
        ("CON .txt", "_CON .txt"),  # reserved stem with a trailing space before the extension
        ("CONIN$", "_CONIN$"),  # console device names are reserved ($ is otherwise legal)
        ("conout$.log", "_conout$.log"),  # case-insensitive, with extension
    ],
)
def test_safe_segment_coerces_and_suffixes_changed_input(value, base):
    out = platform_util.safe_segment(value)
    assert out != value
    assert out.startswith(base + "-")  # sanitized base + collision-suffix digest
    assert _is_legal_segment(out)


def test_safe_segment_distinct_dirty_keys_never_collide():
    # same sanitized base but different raw input must not share a segment (would
    # otherwise cross-wire two stories' task dirs / logs / feedback files)
    a = platform_util.safe_segment("a:b")
    b = platform_util.safe_segment("a?b")
    assert a.startswith("a_b-") and b.startswith("a_b-")
    assert a != b


def test_safe_segment_caps_length():
    out = platform_util.safe_segment("x" * 500)
    assert len(out) <= platform_util._MAX_SEGMENT
    assert _is_legal_segment(out)


def test_dirty_story_key_segment_is_creatable(tmp_path):
    # the sanitized segment a consumer builds a dir from must be creatable on this OS
    from bmad_loop import resolve

    d = resolve._story_dir(tmp_path, 'a<b>:c."')
    d.mkdir(parents=True)
    assert d.is_dir()
