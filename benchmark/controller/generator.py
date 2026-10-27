"""Measured loopback load-generator capacity proof, separate from application results."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading

from controller.execution import ProcessSampler
from controller.services import open_loop_http
from reporting.report import canonical_hash


def calibrate_generator(data, maximum_offered_rate, fingerprint, *, seconds=10):
    if maximum_offered_rate <= 0 or seconds < 1:
        raise ValueError("Generator calibration needs a positive maximum rate and >=1 second")
    # A 64KiB JSON response and parsed equality exercise the most expensive
    # declared baseline HTTP body. The fixture runs in this controller, making
    # the CPU observation conservative: it includes both fixture and generator.
    expected = (Path(data) / "document-65536.json").read_bytes()
    value = json.loads(expected)
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length",str(len(expected)))
            self.end_headers()
            self.wfile.write(expected)
        def log_message(self,*_):
            pass
    server = ThreadingHTTPServer(("127.0.0.1",0),Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever,kwargs={"poll_interval":.1},daemon=True)
    thread.start()
    try:
        import os
        sampler = ProcessSampler(os.getpid())
        try:
            sampler.start()
            result = open_loop_http(f"http://127.0.0.1:{server.server_address[1]}/",maximum_offered_rate*2,seconds,expected,
                                    validator=lambda payload:json.loads(payload)==value)
            resources = sampler.finish()
        finally:
            sampler.close()
        delays = sorted(item["queue_ms"] for item in result["observations"] if "queue_ms" in item)
        p99 = delays[int((len(delays)-1)*.99)] if delays else None
        # A tight dispatch-delay gate verifies the actual offered schedule. CPU
        # headroom includes the fixture and must leave at least half one core.
        proof = {"fingerprint":fingerprint,"maximum_offered_rate":maximum_offered_rate,"tested_rate":maximum_offered_rate*2,
                 "seconds":seconds,"successful":result["successful"],"scheduled":result["scheduled"],"unsent":result["unsent"],
                 "failures":result["failures"],"dispatch_p99_ms":p99,"resources":resources,
                 "passed":result["failures"]==0 and p99 is not None and p99<=2 and resources["cpu_seconds"]/seconds<.5,
                 "boundary":"64KiB parsed JSON at twice the largest frozen offered rate; fixture CPU included conservatively",
                 "observations":result["observations"]}
        proof["proof_hash"] = canonical_hash(proof)
        return proof
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def verify_generator(proof, fingerprint, offered_rate):
    if not isinstance(proof,dict):
        return False
    unsigned = {key:value for key,value in proof.items() if key!="proof_hash"}
    return (proof.get("proof_hash")==canonical_hash(unsigned) and proof.get("fingerprint")==fingerprint
            and proof.get("passed") is True and proof.get("maximum_offered_rate",0)>=offered_rate)
