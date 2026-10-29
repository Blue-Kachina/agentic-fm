"""test_companion_export_progress.py - POST /progress, the export step's progress (roadmap P3-12).

A real companion server runs on a free port in a thread. Timing is checked through
_record_export_progress with given clock values, so no test sleeps.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request

import pytest

import companion_server as cs


@pytest.fixture(autouse=True)
def clean_state():
    with cs._export_lock:
        cs._export_progress.clear()
        cs._export_file_starts.clear()
    yield
    with cs._export_lock:
        cs._export_progress.clear()
        cs._export_file_starts.clear()


@pytest.fixture
def server():
    """Base URL of a companion server on a free port."""
    httpd = cs.ThreadingHTTPServer(("127.0.0.1", 0), cs.CompanionHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def _post(base: str, body, raw: bytes | None = None) -> tuple:
    data = raw if raw is not None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(f"{base}/progress", data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _event(event="file_start", index=3, total=12, file="Invoices.fmp12", solution="Invoices", **extra):
    return {"solution_name": solution, "stage": "export", "event": event, "file": file,
            "index": index, "total": total, **extra}


def test_good_event_is_logged_and_recorded(server, caplog):
    caplog.set_level(logging.INFO, logger="companion_server")
    status, body = _post(server, _event())
    assert status == 200
    assert body["success"] is True and body["event"] == "file_start"
    assert "Exporting 3/12 Invoices.fmp12" in caplog.text

    state = cs._get_export_progress("INVOICES")       # the name is matched without case
    assert state["solution_name"] == "Invoices"
    assert (state["stage"], state["event"], state["file"], state["index"], state["total"]) == \
           ("export", "file_start", "Invoices.fmp12", 3, 12)
    assert state["done"] is False and state["files"] == []
    assert isinstance(state["started_at"], float) and state["updated_at"] >= state["started_at"]


def test_numbers_may_arrive_as_text(server):
    status, _ = _post(server, _event(index="3", total=" 12 "))
    assert status == 200
    state = cs._get_export_progress("Invoices")
    assert (state["index"], state["total"]) == (3, 12)


def test_done_may_have_an_empty_file(server, caplog):
    caplog.set_level(logging.INFO, logger="companion_server")
    _post(server, _event(index=1, total=1))
    _post(server, _event("file_done", index=1, total=1))
    status, body = _post(server, _event("done", index=0, total=0, file=""))
    assert status == 200 and body["done"] is True
    state = cs._get_export_progress("Invoices")
    assert state["done"] is True and (state["index"], state["total"]) == (1, 1)
    assert "Export done: 1 file(s) in" in caplog.text


@pytest.mark.parametrize("body, needle", [
    ({k: v for k, v in _event().items() if k != "solution_name"}, "solution_name"),
    (_event(solution="  "), "solution_name"),
    ({k: v for k, v in _event().items() if k != "stage"}, "stage"),
    (_event(event="started"), "event"),
    ({k: v for k, v in _event().items() if k != "event"}, "event"),
    (_event(file=""), "file"),
    (_event(file=None), "file"),
    (_event(index=-1), "index"),
    (_event(index="three"), "index"),
    (_event(index=2.5), "index"),
    (_event(index=True), "index"),
    ({k: v for k, v in _event().items() if k != "total"}, "total"),
    (_event(index=13, total=12), "between 1 and total"),
    (_event(index=0, total=12), "between 1 and total"),
    ([_event()], "JSON object"),
])
def test_bad_events_are_refused_and_change_nothing(server, body, needle):
    _post(server, _event("file_start", index=1, total=2, file="Contacts.fmp12"))
    before = cs._get_export_progress()
    status, response = _post(server, body)
    assert status == 400
    assert response["success"] is False and needle in response["error"]
    assert cs._get_export_progress() == before


def test_invalid_json_is_refused(server):
    status, response = _post(server, None, raw=b"{not json")
    assert status == 400 and "Invalid request" in response["error"]
    assert cs._get_export_progress() == {}


def test_file_done_takes_its_time_from_the_matching_file_start(caplog):
    caplog.set_level(logging.INFO, logger="companion_server")
    cs._record_export_progress(_event(index=1, total=2, file="Contacts.fmp12"), now=100.0)
    cs._record_export_progress(_event("file_done", index=1, total=2, file="Contacts.fmp12"), now=103.1)
    cs._record_export_progress(_event(index=2, total=2), now=103.2)
    state = cs._record_export_progress(_event("file_done", index=2, total=2), now=111.6)
    assert "Exported 2/2 Invoices.fmp12 (8.4s)" in caplog.text
    assert state["files"] == [{"file": "Contacts.fmp12", "seconds": 3.1},
                              {"file": "Invoices.fmp12", "seconds": 8.4}]
    state = cs._record_export_progress(_event("done", index=2, total=2, file=""), now=112.0)
    assert state["done"] is True and state["started_at"] == 100.0
    assert "Export done: 2 file(s) in 12.0s" in caplog.text


def test_file_done_without_its_start_has_no_time(caplog):
    caplog.set_level(logging.INFO, logger="companion_server")
    state = cs._record_export_progress(_event("file_done", index=2, total=3), now=5.0)
    assert state["files"] == [{"file": "Invoices.fmp12", "seconds": None}]
    assert "Exported 2/3 Invoices.fmp12 (no matching file_start" in caplog.text


def test_a_new_run_starts_with_file_one_or_after_done():
    cs._record_export_progress(_event(index=1, total=1), now=1.0)
    cs._record_export_progress(_event("file_done", index=1, total=1), now=2.0)
    cs._record_export_progress(_event("done", index=1, total=1, file=""), now=2.5)
    state = cs._record_export_progress(_event("done", index=1, total=1, file=""), now=2.6)
    assert len(state["files"]) == 1 and state["started_at"] == 1.0   # a repeated done is no new run

    state = cs._record_export_progress(_event(index=2, total=3), now=10.0)   # any file event after done
    assert state["started_at"] == 10.0 and state["files"] == [] and state["done"] is False
    cs._record_export_progress(_event("file_done", index=2, total=3), now=11.0)
    state = cs._record_export_progress(_event(index=1, total=3), now=20.0)   # file 1 restarts mid-run
    assert state["started_at"] == 20.0 and state["files"] == []


def test_two_solutions_at_once(server):
    def run(solution: str, total: int) -> None:
        for i in range(1, total + 1):
            name = f"{solution}_{i}.fmp12"
            assert _post(server, _event(index=i, total=total, file=name, solution=solution))[0] == 200
            assert _post(server, _event("file_done", index=i, total=total, file=name, solution=solution))[0] == 200
        assert _post(server, _event("done", index=total, total=total, file="", solution=solution))[0] == 200

    threads = [threading.Thread(target=run, args=("Invoices", 4)),
               threading.Thread(target=run, args=("Contacts", 3))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)

    every = cs._get_export_progress()
    assert set(every) == {"invoices", "contacts"}
    assert [f["file"] for f in every["invoices"]["files"]] == [f"Invoices_{i}.fmp12" for i in range(1, 5)]
    assert [f["file"] for f in every["contacts"]["files"]] == [f"Contacts_{i}.fmp12" for i in range(1, 4)]
    assert all(isinstance(f["seconds"], float) for s in every.values() for f in s["files"])
    assert every["invoices"]["done"] and every["contacts"]["done"]
    assert (every["contacts"]["index"], every["contacts"]["total"]) == (3, 3)


def test_getter_returns_copies():
    cs._record_export_progress(_event("file_done", index=1, total=1), now=1.0)
    copy = cs._get_export_progress("Invoices")
    copy["files"].append("x")
    assert cs._get_export_progress("Invoices")["files"] == [{"file": "Invoices.fmp12", "seconds": None}]
    assert cs._get_export_progress("Nope") is None


def test_progress_counts_as_activity(server, monkeypatch):
    monkeypatch.setattr(cs, "_last_activity", time.monotonic() - 1000)
    _post(server, _event())
    assert cs._seconds_since_activity() < 100
