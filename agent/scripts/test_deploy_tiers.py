"""test_deploy_tiers.py - deploy.py picks its tiers by the companion's platform (roadmap P4-06).

Tiers 2 and 3 auto-paste through AppleScript, so they run only when the companion
reports macOS; on Windows a Tier 2 or 3 request is served as Tier 1 with a reason.
The companion is faked (its /health and the POSTs deploy makes), so these tests run
the same on every OS. test_deploy.py is the interactive suite run against FileMaker.
"""

from __future__ import annotations

import sys

import pytest

import deploy as d

STEPS = ('<fmxmlsnippet type="FMObjectList"><Step enable="True" id="89" name="# (comment)">'
         '<Text>a ≠ b</Text></Step></fmxmlsnippet>')


class FakeCompanion:
    """Answers deploy's /health and records each POST (path, payload)."""

    def __init__(self, platform=None, plugin=None):
        self.health = {"status": "ok"}
        if platform:
            self.health["platform"] = platform
        if plugin:
            self.health["plugin"] = plugin
        self.posts = []
        self.health_calls = 0

    def get_health(self, url):
        self.health_calls += 1
        return dict(self.health)

    def post(self, url, payload, timeout=15):
        self.posts.append((url.rsplit("/", 1)[1], payload))
        return {"success": True}

    @property
    def paths(self):
        return [p for p, _ in self.posts]


@pytest.fixture
def xml_file(tmp_path):
    path = tmp_path / "Steps.xml"
    path.write_text(STEPS, encoding="utf-8")
    return str(path)


@pytest.fixture
def companion(monkeypatch):
    def make(platform=None, plugin=None):
        fake = FakeCompanion(platform, plugin)
        monkeypatch.setattr(d, "_companion_health", fake.get_health)
        monkeypatch.setattr(d, "_post_json", fake.post)
        return fake

    monkeypatch.setattr(d, "_load_config", lambda: dict(d.DEFAULT_CONFIG))
    monkeypatch.setattr(d, "_resolve_target_file", lambda config: None)
    monkeypatch.setattr(d, "_is_local_macos", lambda: False)
    monkeypatch.setattr(d, "_check_accessibility",
                        lambda: pytest.fail("the Accessibility check must not run"))
    return make


# --- why auto-paste can't run ---

@pytest.mark.parametrize("platform", [None, "", "macos"])
def test_auto_paste_is_allowed_on_macos_or_when_unknown(platform):
    assert d._auto_paste_unavailable(platform) is None


@pytest.mark.parametrize("platform, name", [("windows", "Windows"), ("linux", "Linux")])
def test_auto_paste_is_mac_only(platform, name):
    reason = d._auto_paste_unavailable(platform)
    assert "Mac only" in reason and f"runs on {name}" in reason


# --- Windows: clipboard only ---

@pytest.mark.parametrize("tier", [2, 3])
def test_windows_serves_tier_2_and_3_as_tier_1(tier, companion, xml_file):
    fake = companion("windows")
    result = d.deploy(xml_file, "My Script", tier=tier)

    assert fake.paths == ["clipboard"]
    assert fake.posts[0][1] == {"xml": STEPS}
    assert result["success"] is True and result["tier_used"] == 1
    assert result["fallback_from"] == tier
    assert "Mac only" in result["fallback_reason"]
    assert "Ctrl+A" in result["instructions"] and "Ctrl+V" in result["instructions"]
    assert "⌘" not in result["instructions"]
    step_1 = {2: "open 'My Script'", 3: "create a script named 'My Script'"}[tier]
    assert f"  1. In FM Pro {step_1} in Script Workspace" in result["instructions"]


def test_windows_tier_1_names_ctrl(companion, xml_file):
    companion("windows")
    result = d.deploy(xml_file, tier=1)
    assert result == {"success": True, "tier_used": 1,
                      "instructions": "Script loaded to clipboard.\n"
                                      "  Paste (Ctrl+V) into the target script in Script Workspace."}


def test_windows_reports_a_clipboard_failure(companion, xml_file, monkeypatch):
    companion("windows")
    monkeypatch.setattr(d, "_post_json", lambda url, payload, timeout=15: {"success": False, "error": "busy"})
    result = d.deploy(xml_file, "My Script", tier=2)

    assert result["success"] is False and result["error"] == "busy"
    assert result["fallback_from"] == 2


# --- macOS: unchanged ---

def test_macos_tier_1_names_cmd(companion, xml_file):
    companion("macos")
    result = d.deploy(xml_file, "My Script", tier=1)
    assert "⌘A" in result["instructions"] and "⌘V" in result["instructions"]
    assert "fallback_from" not in result


def test_macos_tier_2_auto_pastes(companion, xml_file):
    fake = companion("macos")
    result = d.deploy(xml_file, "My Script", tier=2)

    assert fake.paths == ["clipboard", "trigger", "trigger"]
    assert fake.posts[1][1]["script"] == "Agentic-fm Paste"
    assert "raw_applescript" in fake.posts[2][1]
    assert result == {"success": True, "tier_used": 2, "message": "Script steps replaced 'My Script' via Tier 2."}


def test_macos_tier_3_creates_the_script(companion, xml_file):
    fake = companion("macos")
    result = d.deploy(xml_file, "My Script", tier=3)

    assert fake.paths == ["clipboard", "trigger"]
    assert 'keystroke "My Script"' in fake.posts[1][1]["raw_applescript"]
    assert result["tier_used"] == 3 and "fallback_from" not in result


def test_unknown_platform_tries_tier_2_as_before(companion, xml_file):
    fake = companion(None)
    result = d.deploy(xml_file, "My Script", tier=2)
    assert fake.paths == ["clipboard", "trigger", "trigger"]
    assert result["tier_used"] == 2


# --- /health is read once ---

def test_health_is_read_once_and_the_plugin_steer_still_shows(companion, xml_file, capsys):
    fake = companion("windows", plugin={"usable": True})
    d.deploy(xml_file, tier=1)
    assert fake.health_calls == 1
    assert "AgenticFM plug-in is usable" in capsys.readouterr().err


def test_companion_health_is_empty_when_unreachable():
    assert d._companion_health("http://127.0.0.1:9") == {}


# --- CLI ---

def _run_main(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["deploy.py", *argv])
    with pytest.raises(SystemExit) as exc:
        d.main()
    return exc.value.code


def test_cli_on_windows_skips_the_replace_prompt(companion, xml_file, monkeypatch, capsys):
    companion("windows")
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("must not prompt on Windows"))
    assert _run_main(monkeypatch, xml_file, "My Script", "--tier", "2") == 0

    out, err = capsys.readouterr()
    assert "Select all steps (Ctrl+A)" in out
    assert "(Fell back from Tier 2: Auto-paste (Tier 2 and 3) is Mac only" in err


def test_cli_on_macos_still_asks_before_replacing(companion, xml_file, monkeypatch, capsys):
    companion("macos")
    asked = []
    monkeypatch.setattr("builtins.input", lambda prompt="": asked.append(prompt) or "c")
    assert _run_main(monkeypatch, xml_file, "My Script", "--tier", "2") == 0
    assert asked == ["Choice [r/a/c]: "]
    assert "Cancelled." in capsys.readouterr().out
