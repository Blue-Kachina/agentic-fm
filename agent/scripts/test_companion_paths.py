"""test_companion_paths.py - paths FileMaker sends to the companion server (roadmap P3-06).

On native Windows (and macOS) the companion uses the path FileMaker sends unchanged; the WSL
translation (/C:/... -> /mnt/c/...) and the PowerShell + wslpath rebuild of a '?' export path
run only under WSL. The platform is faked, so these tests run the same on every OS.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

import companion_server as cs

DRIVE_PATH = r"C:\Users\x\Desktop\Sol.xml"
UNC_PATH = r"\\server\share\Sol.xml"


def _fake_platform(monkeypatch, name: str, release: str = "", distro: str | None = None) -> None:
    monkeypatch.setattr(sys, "platform", name)
    monkeypatch.setattr(cs.platform, "uname", lambda: SimpleNamespace(release=release))
    if distro is None:
        monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
    else:
        monkeypatch.setenv("WSL_DISTRO_NAME", distro)


@pytest.fixture
def native_windows(monkeypatch):
    _fake_platform(monkeypatch, "win32", release="10")


@pytest.fixture
def wsl(monkeypatch):
    _fake_platform(monkeypatch, "linux", release="5.15.167.4-microsoft-standard-WSL2")


@pytest.fixture
def no_wsl_calls(monkeypatch):
    """Fail the test if anything on the WSL branch runs."""
    def boom(*args, **kwargs):
        raise AssertionError(f"WSL branch entered: {args!r}")
    monkeypatch.setattr(cs, "_wsl_translate_path", boom)
    monkeypatch.setattr(cs, "_resolve_export_path_for_wsl", boom)
    monkeypatch.setattr(cs.subprocess, "run", boom)


# -- detection -----------------------------------------------------------------------

def test_native_windows_is_not_wsl(native_windows):
    assert cs._is_native_windows()
    assert not cs._is_wsl()


def test_wsl_detected_from_kernel_release(wsl):
    assert cs._is_wsl()
    assert not cs._is_native_windows()


def test_wsl_detected_from_distro_env(monkeypatch):
    _fake_platform(monkeypatch, "linux", release="6.1.0-generic", distro="Ubuntu")
    assert cs._is_wsl()


def test_plain_linux_and_macos_are_not_wsl(monkeypatch):
    _fake_platform(monkeypatch, "linux", release="6.1.0-generic")
    assert not cs._is_wsl()
    _fake_platform(monkeypatch, "darwin", release="24.0.0")
    assert not cs._is_wsl()


# -- native Windows: unchanged -------------------------------------------------------

@pytest.mark.parametrize("path", [DRIVE_PATH, UNC_PATH, "/C:/Users/x/Desktop/Sol.xml"])
def test_native_windows_path_passes_through(native_windows, no_wsl_calls, path):
    assert cs._translate_incoming_path(path) == path


@pytest.mark.parametrize("path", [DRIVE_PATH, UNC_PATH])
def test_native_windows_export_path_passes_through(native_windows, no_wsl_calls, path):
    assert cs._incoming_export_path(path, "Sol") == path


def test_native_windows_question_mark_is_unresolved(native_windows, no_wsl_calls):
    assert cs._incoming_export_path("?", "Sol") == ""
    assert "WSL" not in cs._unresolved_export_path_error("export_path")


def test_macos_path_passes_through(monkeypatch, no_wsl_calls):
    _fake_platform(monkeypatch, "darwin", release="24.0.0")
    assert cs._translate_incoming_path("/Users/x/Desktop/Sol.xml") == "/Users/x/Desktop/Sol.xml"


# -- WSL: unchanged behaviour --------------------------------------------------------

def test_wsl_still_translates(wsl):
    assert cs._translate_incoming_path("/C:/Users/x/Desktop/Sol.xml") == "/mnt/c/Users/x/Desktop/Sol.xml"
    assert cs._incoming_export_path("/C:/Users/x/Desktop/Sol.xml", "Sol") == "/mnt/c/Users/x/Desktop/Sol.xml"


def test_wsl_question_mark_uses_rebuild(wsl, monkeypatch):
    monkeypatch.setattr(cs, "_resolve_export_path_for_wsl", lambda name: f"/mnt/c/Users/x/Desktop/{name}.xml")
    assert cs._incoming_export_path("?", "Sol") == "/mnt/c/Users/x/Desktop/Sol.xml"
