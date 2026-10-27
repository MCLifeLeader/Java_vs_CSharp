"""Real loopback fixtures and offered-arrival accounting, with tiny test traffic."""

import http.client
from pathlib import Path
import socket
import ssl
import sys
import tempfile
import unittest
from urllib.parse import urlsplit

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

    def test_rejects_unlisted_remote_host(self):
        with self.assertRaisesRegex(ValueError, "loopback"):
            open_loop_http("http://example.com/", 1, 1, b"x")


if __name__ == "__main__":
    unittest.main()
