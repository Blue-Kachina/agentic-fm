"""conftest.py - shared pytest setup for agent/scripts.

Every test session gets its own companion state folder ($COMPANION_STATE_DIR), so no test (nor a
companion it starts as a subprocess, which inherits the environment) writes the log file, state
file or job records into the real per-user folder. Tests that need their own folder still set
COMPANION_STATE_DIR with monkeypatch.setenv.
"""

from __future__ import annotations

import os

import pytest

STATE_DIR_ENV = "COMPANION_STATE_DIR"     # companion_state.STATE_DIR_ENV


@pytest.fixture(scope="session", autouse=True)
def companion_state_dir(tmp_path_factory):
    """$COMPANION_STATE_DIR set to a session temp folder for the whole run, then restored."""
    folder = tmp_path_factory.mktemp("companion_state")
    previous = os.environ.get(STATE_DIR_ENV)
    os.environ[STATE_DIR_ENV] = str(folder)
    try:
        yield str(folder)
    finally:
        if previous is None:
            os.environ.pop(STATE_DIR_ENV, None)
        else:
            os.environ[STATE_DIR_ENV] = previous
