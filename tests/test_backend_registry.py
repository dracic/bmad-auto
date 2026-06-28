"""Backend-registry selection proof.

The multiplexer seam selects its transport backend through a registry
(:func:`~automator.adapters.multiplexer.register_multiplexer`) rather than a
hardcoded constructor, so a new OS/backend is a registration — not a core edit.
These tests pin selection: by platform match, by the ``BMAD_AUTO_MUX_BACKEND``
override, the safe tmux fallback, and the lru_cache gotcha. Backends register a
sentinel ``object()`` factory so a test need not implement the whole ABC.
"""

import sys

import pytest

from automator.adapters import multiplexer as m
from automator.adapters.tmux_backend import TmuxMultiplexer


@pytest.fixture
def fresh_registry(monkeypatch):
    """Isolate the global registry + lru_cache: snapshot, clear, restore. The env
    override is removed so a test opts in explicitly. Teardown restores the real
    tmux registry so unrelated tests see normal selection."""
    monkeypatch.delenv("BMAD_AUTO_MUX_BACKEND", raising=False)
    saved_backends = list(m._BACKENDS)
    saved_loaded = m._BUILTINS_LOADED
    m._BACKENDS.clear()
    m._BUILTINS_LOADED = False
    m.get_multiplexer.cache_clear()
    yield m
    m._BACKENDS[:] = saved_backends
    m._BUILTINS_LOADED = saved_loaded
    m.get_multiplexer.cache_clear()


def test_default_is_tmux(fresh_registry):
    """No override, POSIX host → tmux, selected via the loop's platform match (the
    builtin registers ``matches=p != 'win32'``), not just the bottom fallback."""
    assert isinstance(fresh_registry.get_multiplexer(), TmuxMultiplexer)


def test_env_override_selects_named_backend(fresh_registry, monkeypatch):
    """``BMAD_AUTO_MUX_BACKEND`` resolves a backend by name without monkeypatching
    sys.platform. ``matches`` returns False here, so only the name path can pick it."""
    sentinel = object()
    fresh_registry.register_multiplexer("fake", lambda p: False, lambda: sentinel)
    monkeypatch.setenv("BMAD_AUTO_MUX_BACKEND", "fake")
    fresh_registry.get_multiplexer.cache_clear()
    assert fresh_registry.get_multiplexer() is sentinel


def test_env_override_tmux_returns_tmux(fresh_registry, monkeypatch):
    """Forcing the default by name still works (name match short-circuits)."""
    monkeypatch.setenv("BMAD_AUTO_MUX_BACKEND", "tmux")
    fresh_registry.get_multiplexer.cache_clear()
    assert isinstance(fresh_registry.get_multiplexer(), TmuxMultiplexer)


def test_unknown_forced_name_falls_back_to_tmux(fresh_registry, monkeypatch):
    """An unregistered forced name matches nothing in the loop and lands on the
    safe tmux fallback rather than raising."""
    monkeypatch.setenv("BMAD_AUTO_MUX_BACKEND", "nope")
    fresh_registry.get_multiplexer.cache_clear()
    assert isinstance(fresh_registry.get_multiplexer(), TmuxMultiplexer)


def test_match_based_selection_wins_by_order(fresh_registry):
    """A backend registered before the builtins whose ``matches`` accepts the
    current platform is selected by auto-match (no override), proving platform
    selection and registration-order precedence over tmux."""
    sentinel = object()
    fresh_registry.register_multiplexer("fake", lambda p: p == sys.platform, lambda: sentinel)
    fresh_registry.get_multiplexer.cache_clear()
    assert fresh_registry.get_multiplexer() is sentinel


def test_get_multiplexer_is_cached(fresh_registry):
    """One process-wide instance: repeated calls return the same object."""
    fresh_registry.get_multiplexer.cache_clear()
    assert fresh_registry.get_multiplexer() is fresh_registry.get_multiplexer()
