"""Controller-owned loopback fixtures. No shared Toolbox state or remote host access."""

from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import json
from pathlib import Path
import socketserver
import ssl
import threading
import time
from urllib.parse import parse_qs, urlsplit


class DelayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        parsed = urlsplit(self.path)
        if parsed.path != "/delay":
            self.send_error(404)
            return
        try:
            milliseconds = int(parse_qs(parsed.query).get("ms", ["2"])[0])
            if not 0 <= milliseconds <= 5000:
                raise ValueError()
        except ValueError:
            self.send_error(400)
            return
        time.sleep(milliseconds / 1000)
        body = b"benchmark-delay-ok"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


class EchoHandler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(15)
        while True:
            try:
                data = self.request.recv(65536)
                if not data:
                    return
                self.request.sendall(data)
            except (ConnectionResetError, BrokenPipeError, ssl.SSLEOFError, TimeoutError):
                return


class EchoServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    block_on_close = False


class TlsEchoServer(EchoServer):
    def __init__(self, address, context):
        self.context = context
        super().__init__(address, EchoHandler)

    def get_request(self):
        socket, address = super().get_request()
        socket.settimeout(15)
        try:
            return self.context.wrap_socket(socket, server_side=True), address
        except BaseException:
            socket.close()
            raise


class LocalServices:
    def __init__(self, root, data_dir):
        self.root, self.data_dir = Path(root), Path(data_dir)
        self.servers, self.threads, self.running = [], [], set()
        self.manifest = None

    def __enter__(self):
        cert = self.root / "infrastructure" / "tls" / "localhost.crt"
        key = self.root / "infrastructure" / "tls" / "localhost.key"
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = context.maximum_version = ssl.TLSVersion.TLSv1_3
        context.load_cert_chain(cert, key)
        try:
            delay = ThreadingHTTPServer(("127.0.0.1", 0), DelayHandler)
            self.servers.append(delay)
            delay.daemon_threads = True
            echo = EchoServer(("127.0.0.1", 0), EchoHandler)
            self.servers.append(echo)
            tls = TlsEchoServer(("127.0.0.1", 0), context)
            self.servers.append(tls)
            for server in self.servers:
                thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
                thread.start()
                self.threads.append(thread)
                self.running.add(server)
            return self._publish(delay, echo, tls, cert)
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def _publish(self, delay, echo, tls, cert):
        self.manifest = {
            "delay_url": f"http://127.0.0.1:{delay.server_address[1]}/delay?ms=2",
            "delay_expected_body": "benchmark-delay-ok", "echo_host": "127.0.0.1",
            "echo_port": echo.server_address[1], "tls_port": tls.server_address[1],
            "tls_certificate": str(cert.resolve()), "tls_protocol": "TLSv1.3",
            "network_boundary": "loopback; measures local protocol stack, not remote bandwidth",
            "echo_framing": "raw byte echo; client sends and consumes the exact known payload length",
        }
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "services.json").write_text(json.dumps(self.manifest, indent=2) + "\n", encoding="utf-8")
        return self

    def __exit__(self, *_):
        errors = []
        for server in self.servers:
            try:
                if server in self.running:
                    server.shutdown()
            except Exception as error:
                errors.append(error)
            finally:
                try:
                    server.server_close()
                except Exception as error:
                    errors.append(error)
        for thread in self.threads:
            thread.join(timeout=2)
            if thread.is_alive():
                errors.append(RuntimeError("Owned fixture thread did not stop"))
        self.running.clear()
        path = self.data_dir / "services.json"
        if self.manifest is not None and path.is_file():
            try:
                if json.loads(path.read_text(encoding="utf-8")) == self.manifest:
                    path.unlink()
            except (OSError, ValueError) as error:
                errors.append(error)
        if errors:
            raise ExceptionGroup("Owned fixture cleanup failed", errors)


def open_loop_http(url, rate, duration_seconds, expected_body, *, method="GET", body=None,
                   content_type="application/json", max_connections=256, deadline_seconds=15,
                   validator=None, expected_status=200, requests=None):
    """Fixed offered schedule; queue delay counts, unsent work stays a failure.

    Standard-library load generation must be calibrated before scored use. A
    saturated generator is an invalid campaign, never a successful fast server.
    """
    parsed = urlsplit(url)
    if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost"):
        raise ValueError("HTTP load fixture permits explicit loopback HTTP endpoints only")
    if rate <= 0 or duration_seconds <= 0 or max_connections <= 0 or deadline_seconds <= 0:
        raise ValueError("Positive rate, duration, connection cap and deadline required")
    count = int(rate * duration_seconds)
    if count < 1:
        raise ValueError("The offered schedule has no arrivals")
    if requests is not None and len(requests) != count:
        raise ValueError("Finite HTTP request plan must match the entire offered arrival count")
    semaphore = threading.BoundedSemaphore(max_connections)
    lock = threading.Lock()
    observations, futures = [], []
    thread_local = threading.local()
    connections = []
    beginning = time.perf_counter()
    path = parsed.path + (("?" + parsed.query) if parsed.query else "")
    def request(index, scheduled):
        started = time.perf_counter()
        connection = getattr(thread_local, "connection", None)
        plan = requests[index] if requests is not None else {}
        try:
            remaining = deadline_seconds - (started - scheduled)
            if remaining <= 0:
                raise TimeoutError("Deadline expired before send")
            if connection is None:
                connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=remaining)
                thread_local.connection = connection
                with lock:
                    connections.append(connection)
            connection.timeout = remaining
            if connection.sock:
                connection.sock.settimeout(remaining)
            connection.request(plan.get("method", method), plan.get("path", path), body=plan.get("body", body), headers={"Content-Type": content_type})
            response = connection.getresponse()
            payload = response.read()
            response_validator = plan.get("validator", validator)
            correct = response.status == plan.get("status", expected_status) and (response_validator(payload) if response_validator else payload == plan.get("expected", expected_body))
            finished = time.perf_counter()
            status = "success" if correct and finished - scheduled <= deadline_seconds else "deadline" if finished - scheduled > deadline_seconds else "incorrect"
            item = {"arrival": index, "status": status, "scheduled_seconds": scheduled - beginning,
                    "latency_ms": (finished - scheduled) * 1000, "queue_ms": (started - scheduled) * 1000,
                    "http_status": response.status, "bytes": len(payload)}
        except Exception as error:
            finished = time.perf_counter()
            item = {"arrival": index, "status": "timeout" if isinstance(error, (TimeoutError, OSError)) else "error",
                    "scheduled_seconds": scheduled - beginning, "latency_ms": (finished - scheduled) * 1000,
                    "queue_ms": (started - scheduled) * 1000, "reason": type(error).__name__}
            if connection:
                connection.close()
                thread_local.connection = None
        finally:
            semaphore.release()
        with lock:
            observations.append(item)
    with ThreadPoolExecutor(max_workers=max_connections, thread_name_prefix="benchmark-load") as executor:
        for index in range(count):
            scheduled = beginning + index / rate
            delay = scheduled - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            if not semaphore.acquire(blocking=False):
                observations.append({"arrival": index, "status": "unsent", "scheduled_seconds": index / rate,
                                     "latency_ms": None, "reason": "Load-generator connection capacity exhausted"})
            else:
                futures.append(executor.submit(request, index, scheduled))
        for future in futures:
            future.result()
    for connection in connections:
        connection.close()
    observations.sort(key=lambda item: item["arrival"])
    successful = [item for item in observations if item["status"] == "success"]
    return {"scheduled": count, "sent": sum(item["status"] != "unsent" for item in observations),
            "successful": len(successful), "failures": count - len(successful),
            "unsent": sum(item["status"] == "unsent" for item in observations),
            "offered_rate": rate, "arrival_window_seconds": duration_seconds,
            "successful_requests_per_second": len(successful) / duration_seconds,
            "error_rate": (count - len(successful)) / count, "observations": observations,
            "connection_cap":max_connections,"deadline_seconds":deadline_seconds,"http_version":"HTTP/1.1","keep_alive":True,
            "clock": "perf_counter", "latency_boundary": "Intended scheduled arrival to complete validated response",
            "generator_capacity_calibrated": False}
