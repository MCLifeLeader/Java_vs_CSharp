"""Real loopback fixtures and offered-arrival accounting, with tiny test traffic."""

import http.client
from pathlib import Path
import socket
import ssl
import sys
import tempfile
import unittest
from urllib.parse import urlsplit
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from controller.services import LocalServices, open_loop_http


class ServiceTests(unittest.TestCase):
    def test_shared_delay_plain_echo_and_tls13_echo(self):
        with tempfile.TemporaryDirectory() as temporary, LocalServices(ROOT, temporary) as services:
            manifest = services.manifest
            url = urlsplit(manifest["delay_url"])
            connection = http.client.HTTPConnection(url.hostname, url.port, timeout=5)
            connection.request("GET", url.path + "?" + url.query)
            response = connection.getresponse()
            self.assertEqual(200, response.status)
            self.assertEqual(b"benchmark-delay-ok", response.read())
            connection.close()
            payload = b"canonical echo" * 100
            with socket.create_connection(("127.0.0.1", manifest["echo_port"]), timeout=5) as stream:
                stream.sendall(payload)
                self.assertEqual(payload, stream.recv(len(payload)))
            context = ssl.create_default_context(cafile=manifest["tls_certificate"])
            context.minimum_version = ssl.TLSVersion.TLSv1_3
            with socket.create_connection(("127.0.0.1", manifest["tls_port"]), timeout=5) as plain:
                with context.wrap_socket(plain, server_hostname="localhost") as secure:
                    self.assertEqual("TLSv1.3", secure.version())
                    secure.sendall(payload)
                    self.assertEqual(payload, secure.recv(len(payload)))

    def test_open_loop_preserves_all_scheduled_arrivals(self):
        with tempfile.TemporaryDirectory() as temporary, LocalServices(ROOT, temporary) as services:
            result = open_loop_http(services.manifest["delay_url"], 10, .3, b"benchmark-delay-ok", max_connections=2)
        self.assertEqual(3, result["scheduled"])
        self.assertEqual(result["scheduled"], result["successful"] + result["failures"])
        self.assertEqual(3, len(result["observations"]))
        self.assertTrue(all(row["latency_ms"] > 0 for row in result["observations"]))
        self.assertFalse(result["generator_capacity_calibrated"])

    def test_partial_start_failure_closes_already_bound_fixture_sockets(self):
        from controller import services as module
        opened = []
        http_server, echo_server = module.ThreadingHTTPServer, module.EchoServer
        def make_http(*args, **kwargs):
            server = http_server(*args, **kwargs)
            opened.append(server)
            return server
        def make_echo(*args, **kwargs):
            server = echo_server(*args, **kwargs)
            opened.append(server)
            return server
        with tempfile.TemporaryDirectory() as temporary:
            fixture = LocalServices(ROOT, temporary)
            with patch.object(module, "ThreadingHTTPServer", side_effect=make_http), patch.object(module, "EchoServer", side_effect=make_echo), patch.object(module, "TlsEchoServer", side_effect=RuntimeError("Injected TLS startup failure")):
                with self.assertRaisesRegex(RuntimeError, "Injected TLS"):
                    fixture.__enter__()
            self.assertEqual(2, len(opened))
            self.assertTrue(all(server.socket.fileno() == -1 for server in opened))
            self.assertFalse((Path(temporary) / "services.json").exists())

    def test_teardown_failure_still_closes_other_fixtures_and_removes_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = LocalServices(ROOT, temporary)
            fixture.__enter__()
            original = fixture.servers[0].shutdown
            def fail_after_shutdown():
                original()
                raise OSError("Injected shutdown failure")
            with patch.object(fixture.servers[0], "shutdown", side_effect=fail_after_shutdown):
                with self.assertRaises(ExceptionGroup):
                    fixture.__exit__(None, None, None)
            self.assertTrue(all(server.socket.fileno() == -1 for server in fixture.servers))
            self.assertTrue(all(not thread.is_alive() for thread in fixture.threads))
            self.assertFalse((Path(temporary) / "services.json").exists())

    def test_rejects_unlisted_remote_host(self):
        with self.assertRaisesRegex(ValueError, "loopback"):
            open_loop_http("http://example.com/", 1, 1, b"x")


if __name__ == "__main__":
    unittest.main()
