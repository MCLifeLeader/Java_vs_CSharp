"""Download and verify the pinned, private-to-this-worktree Maven distribution."""
import hashlib
import json
from pathlib import Path
import stat
import sys
import urllib.request
import zipfile

VERSION = "3.9.11"
SHA256 = "0d7125e8c91097b36edb990ea5934e6c68b4440eef4ea96510a0f6815e7eeadb"
URL = f"https://repo.maven.apache.org/maven2/org/apache/maven/apache-maven/{VERSION}/apache-maven-{VERSION}-bin.zip"
ROOT = Path(__file__).resolve().parents[2]


def bootstrap():
    sys.path.insert(0,str(ROOT))
    from controller.toolchain import activate_toolchain
    activate_toolchain(ROOT)
    tools = ROOT / ".work" / "tools"
    tools.mkdir(parents=True, exist_ok=True)
    archive = tools / f"apache-maven-{VERSION}-bin.zip"
    if not archive.is_file():
        with urllib.request.urlopen(URL, timeout=60) as response:
            payload = response.read()
        if hashlib.sha256(payload).hexdigest() != SHA256:
            raise ValueError("Maven archive SHA-256 differs")
        archive.write_bytes(payload)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != SHA256:
        raise ValueError("Cached Maven archive SHA-256 differs")
    destination = tools.resolve()
    with zipfile.ZipFile(archive) as package:
        for member in package.infolist():
            if not (destination / member.filename).resolve().is_relative_to(destination):
                raise ValueError("Archive member escapes tools directory")
        package.extractall(destination)
    executable = destination / f"apache-maven-{VERSION}" / "bin" / "mvn"
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    return {"version": VERSION, "archive_sha256": SHA256,
            "windows": str(executable.with_suffix(".cmd")), "unix": str(executable)}


if __name__ == "__main__":
    print(json.dumps(bootstrap(), indent=2))
