"""test_companion_port.py - a second companion can't share the first one's port.

On Windows, SO_REUSEADDR lets two processes listen on one port, and FileMaker's
requests then reach either companion. ThreadingHTTPServer takes the port
exclusively there, and main() turns the bind error into a clear message.
"""

from __future__ import annotations

import socket
import sys
import threading

import pytest

import companion_server as cs


@pytest.fixture
def server():
    httpd = cs.ThreadingHTTPServer(("127.0.0.1", 0), cs.CompanionHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()


def test_second_companion_cannot_bind_the_same_port(server):
    port = server.server_address[1]
    with pytest.raises(OSError):
        cs.ThreadingHTTPServer(("127.0.0.1", port), cs.CompanionHandler)


@pytest.mark.skipif(sys.platform != "win32", reason="SO_REUSEADDR shares a listening port only on Windows")
def test_a_reuseaddr_socket_cannot_bind_after_it(server):
    # What an older companion (HTTPServer's default SO_REUSEADDR) would do
    port = server.server_address[1]
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        with pytest.raises(OSError):
            sock.bind(("127.0.0.1", port))
    finally:
        sock.close()


def test_reuse_address_only_off_on_windows():
    assert cs.ThreadingHTTPServer.allow_reuse_address is (sys.platform != "win32")


def test_main_explains_a_port_in_use(server, monkeypatch, caplog):
    port = server.server_address[1]
    monkeypatch.setattr(sys, "argv", ["companion_server.py", "--port", str(port), "--no-live"])
    monkeypatch.setattr(cs, "_load_companion_config", lambda: {})
    monkeypatch.delenv("COMPANION_BIND_HOST", raising=False)

    def bound_anyway(self, *args, **kwargs):     # fail fast instead of serving forever
        raise AssertionError("a second companion bound the port that is in use")

    monkeypatch.setattr(cs.ThreadingHTTPServer, "serve_forever", bound_anyway)
    with pytest.raises(SystemExit) as exit_info:
        cs.main()
    assert exit_info.value.code == 1
    message = caplog.text
    assert f"Port {port} on 127.0.0.1 is already in use" in message
    assert ("findstr" if sys.platform == "win32" else "lsof") in message
