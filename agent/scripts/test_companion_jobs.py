"""test_companion_jobs.py - conversion records kept on disk (roadmap P3-23).

Each finished job writes jobs/<job id>.json, jobs/<job id>.log and jobs/last_job.json in the
state folder ($COMPANION_STATE_DIR here, a test folder), plus _companion_job.json in the output
folder. A fake fm-saxml (a short Python script) stands in for the real one.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading

import pytest

import companion_server as cs

FAKE_FM_SAXML = r'''
import json, os, sys
docs_root, solution, mode = sys.argv[1], sys.argv[2], sys.argv[3]
print("Converting " + solution, flush=True)
print(json.dumps({"event": "stage_start", "stage": "read", "total": 1, "index": 0, "elapsed": 0.1}), flush=True)
sys.stderr.write("a warning on stderr\n")
sys.stderr.flush()
if mode == "fail":
    print("x" * 6000, flush=True)
    sys.stderr.write("fatal: the export is broken\n")
    sys.exit(3)
out = os.path.join(docs_root, solution)
os.makedirs(out, exist_ok=True)
with open(os.path.join(out, "docs.json"), "w", encoding="utf-8") as f:
    json.dump({"solution": solution, "kind": "single", "files": []}, f)
print("Done: " + out, flush=True)
'''


@pytest.fixture(autouse=True)
def state(monkeypatch, tmp_path):
    """A test state folder, no jobs, and a fake fm-saxml. Returns the state folder."""
    folder = tmp_path / "state"
    monkeypatch.setenv("COMPANION_STATE_DIR", str(folder))
    monkeypatch.setattr(cs, "_convert_progress_supported", True)
    with cs._convert_lock:
        cs._convert_jobs.clear()
        cs._convert_outputs.clear()
        cs._convert_commands.clear()
    return folder


@pytest.fixture
def fake(tmp_path, monkeypatch):
    """Install the fake fm-saxml; settings["mode"] is "ok" or "fail"."""
    script = tmp_path / "fake_fm_saxml.py"
    script.write_text(FAKE_FM_SAXML, encoding="utf-8")
    settings = {"mode": "ok"}

    def command(export_path, docs_root, solution_name, **kwargs):
        return [sys.executable, str(script), docs_root, solution_name, settings["mode"]]

    monkeypatch.setattr(cs, "_convert_command", command)
    return settings


def _convert(tmp_path, solution: str = "Invoices") -> dict:
    """Run one conversion to the end (record written) and return the job."""
    export = tmp_path / "export.xml"
    export.write_text("<FMSaveAsXML/>", encoding="utf-8")
    job, running = cs._start_convert_job(solution, str(export), str(tmp_path / "docs"))
    assert running is None
    for thread in threading.enumerate():
        if thread.name == f"convert-{job['job_id']}":
            thread.join(30)
            assert not thread.is_alive()
    return cs._get_convert_job(job["job_id"])


def _record(state, job_id: str) -> dict:
    return json.loads((state / "jobs" / f"{job_id}.json").read_text(encoding="utf-8"))


def test_success_writes_the_record_log_last_job_and_output_copy(tmp_path, state, fake):
    job = _convert(tmp_path)
    assert job["state"] == "succeeded"
    record = _record(state, job["job_id"])
    # The job as GET /convert returns it, plus the command and where the log is
    for key, value in job.items():
        assert record[key] == value
    assert record["command"][0] == sys.executable and record["command"][-1] == "ok"
    assert record["log_file"] == str(state / "jobs" / f"{job['job_id']}.log")
    log_text = (state / "jobs" / f"{job['job_id']}.log").read_text(encoding="utf-8")
    # The whole output: plain lines, stderr, and the progress JSON lines the job's log_tail leaves out
    assert "Converting Invoices" in log_text and "a warning on stderr" in log_text
    assert '"event": "stage_start"' in log_text and "Done: " in log_text
    assert json.loads((state / "jobs" / cs.JOB_RECORD_LAST).read_text(encoding="utf-8")) == record
    in_output = os.path.join(job["output_path"], cs.JOB_RECORD_IN_OUTPUT)
    assert json.loads(open(in_output, encoding="utf-8").read()) == record
    # Nothing held back in memory once written
    assert job["job_id"] not in cs._convert_outputs and job["job_id"] not in cs._convert_commands


def test_fm_saxml_failure_keeps_the_full_output_on_disk(tmp_path, state, fake):
    fake["mode"] = "fail"
    job = _convert(tmp_path)
    assert job["state"] == "failed" and job["exit_code"] == 3
    assert len(job["log_tail"]) == cs.CONVERT_LOG_TAIL_CHARS
    record = _record(state, job["job_id"])
    assert record["state"] == "failed" and record["error"] == "fm-saxml exited with code 3"
    log_text = (state / "jobs" / f"{job['job_id']}.log").read_text(encoding="utf-8")
    assert "x" * 6000 in log_text and "fatal: the export is broken" in log_text
    assert "Converting Invoices" in log_text      # the start, cut from log_tail, is in the .log
    assert not (tmp_path / "docs" / "Invoices" / cs.JOB_RECORD_IN_OUTPUT).exists()


def test_runner_crash_is_recorded_with_its_traceback(tmp_path, state, fake, monkeypatch):
    def boom(output_path):
        raise RuntimeError("boom while summarising")

    monkeypatch.setattr(cs, "_summarize_output", boom)
    job = _convert(tmp_path)
    assert job["state"] == "failed" and job["error"] == "Conversion crashed: boom while summarising"
    record = _record(state, job["job_id"])
    assert record["error"] == job["error"]
    log_text = (state / "jobs" / f"{job['job_id']}.log").read_text(encoding="utf-8")
    assert "Converting Invoices" in log_text
    assert "RuntimeError: boom while summarising" in log_text


def test_a_failed_write_is_logged_and_never_fails_the_job(tmp_path, fake, monkeypatch, caplog):
    def no_folder(create=True):
        raise PermissionError("access denied")

    monkeypatch.setattr(cs.companion_state, "jobs_dir", no_folder)
    with caplog.at_level(logging.WARNING, logger="companion_server"):
        job = _convert(tmp_path)
    assert job["state"] == "succeeded"
    assert "could not write its record" in caplog.text and "access denied" in caplog.text


def test_a_failed_output_copy_is_logged_and_the_jobs_record_stays(tmp_path, state, fake, monkeypatch, caplog):
    real = cs._write_text_atomic

    def no_output_copy(path, text):
        if os.path.basename(path) == cs.JOB_RECORD_IN_OUTPUT:
            raise OSError("read-only folder")
        real(path, text)

    monkeypatch.setattr(cs, "_write_text_atomic", no_output_copy)
    with caplog.at_level(logging.WARNING, logger="companion_server"):
        job = _convert(tmp_path)
    assert job["state"] == "succeeded"
    assert _record(state, job["job_id"])["state"] == "succeeded"
    assert "read-only folder" in caplog.text


def test_records_are_pruned_to_convert_max_jobs(tmp_path, monkeypatch):
    folder = tmp_path / "jobs"
    folder.mkdir()
    for i in range(6):
        for ext in ("json", "log"):
            path = folder / f"job{i}.{ext}"
            path.write_text("{}", encoding="utf-8")
            os.utime(path, (1_000_000 + i, 1_000_000 + i))
    (folder / cs.JOB_RECORD_LAST).write_text("{}", encoding="utf-8")
    os.utime(folder / cs.JOB_RECORD_LAST, (1, 1))       # oldest of all, and still kept
    monkeypatch.setattr(cs, "CONVERT_MAX_JOBS", 3)
    cs._prune_job_records(str(folder))
    assert sorted(os.listdir(folder)) == sorted(
        [cs.JOB_RECORD_LAST] + [f"job{i}.{ext}" for i in (3, 4, 5) for ext in ("json", "log")])


def test_end_active_jobs_records_a_forced_stop(tmp_path, state):
    class FakeProcess:
        pid = 4242
        terminated = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

    process = FakeProcess()
    with cs._convert_lock:
        cs._convert_jobs["running1"] = {"job_id": "running1", "state": "running", "done": False,
                                        "solution_name": "Invoices", "output_path": ""}
        cs._convert_jobs["finished"] = {"job_id": "finished", "state": "succeeded", "done": True,
                                        "solution_name": "Other", "output_path": ""}
        cs._convert_processes["running1"] = process
        cs._convert_outputs["running1"] = "half the output\n"
    try:
        cs._end_active_jobs("stopped by /shutdown")
    finally:
        with cs._convert_lock:
            cs._convert_processes.clear()
    assert process.terminated
    job = cs._get_convert_job("running1")
    assert job["state"] == "failed" and job["done"] is True and job["error"] == "stopped by /shutdown"
    assert _record(state, "running1")["error"] == "stopped by /shutdown"
    assert (state / "jobs" / "running1.log").read_text(encoding="utf-8") == "half the output\n"
    assert cs._get_convert_job("finished")["state"] == "succeeded"
    assert not (state / "jobs" / "finished.json").exists()
