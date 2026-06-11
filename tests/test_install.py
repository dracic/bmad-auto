import json

from automator.install import HOOK_EVENTS, install_into, merge_hooks


def test_merge_hooks_adds_all_events():
    settings, changed = merge_hooks({})
    assert changed
    assert set(HOOK_EVENTS) <= set(settings["hooks"])


def test_merge_hooks_idempotent():
    settings, _ = merge_hooks({})
    again, changed = merge_hooks(settings)
    assert not changed
    for event in HOOK_EVENTS:
        assert len(again["hooks"][event]) == 1


def test_merge_hooks_preserves_existing():
    existing = {
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo hi"}]}]},
        "permissions": {"allow": ["Bash(ls)"]},
    }
    settings, changed = merge_hooks(existing)
    assert changed
    assert settings["permissions"] == {"allow": ["Bash(ls)"]}
    commands = [
        handler["command"]
        for matcher in settings["hooks"]["Stop"]
        for handler in matcher["hooks"]
    ]
    assert "echo hi" in commands
    assert any("bmad_auto_hook" in c for c in commands)


def test_install_into_full(tmp_path):
    assert install_into(tmp_path) == 0
    assert (tmp_path / ".automator" / "bmad_auto_hook.py").is_file()
    assert (tmp_path / ".automator" / "policy.toml").is_file()
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert "Stop" in settings["hooks"]
    assert ".automator/runs/" in (tmp_path / ".gitignore").read_text()

    # second run: idempotent, does not duplicate
    assert install_into(tmp_path) == 0
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert len(settings["hooks"]["Stop"]) == 1
    assert (tmp_path / ".gitignore").read_text().count(".automator/runs/") == 1
