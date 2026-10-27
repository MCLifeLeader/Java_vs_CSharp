"""Install hash-pinned build helpers into benchmark/.work; machine installations stay operator-owned."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import sys
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = {
    "jdk": {
        "url": "https://aka.ms/download-jdk/microsoft-jdk-25.0.3-windows-x64.zip",
        "sha256": "ba3ce9fa6ebb921eccda29b08c2bad26b5f8839080931096c73f306e0dd9856f",
        "archive": "microsoft-jdk-25.0.3-windows-x64.zip", "destination": ".",
    },
    "maven": {
        "url": "https://archive.apache.org/dist/maven/maven-3/3.9.11/binaries/apache-maven-3.9.11-bin.zip",
        "sha256": "0d7125e8c91097b36edb990ea5934e6c68b4440eef4ea96510a0f6815e7eeadb",
        "archive": "apache-maven-3.9.11-bin.zip", "destination": ".",
    },
    "runtime": {
        "url": "https://builds.dotnet.microsoft.com/dotnet/aspnetcore/Runtime/10.0.14/aspnetcore-runtime-10.0.14-win-x64.zip",
        "sha256": "ae61cd4db653c4fec9b4ad74fc8bdb2d735179966d400fdfe64c65f1b36c2014",
        "archive": "aspnetcore-runtime-10.0.14-win-x64.zip", "destination": "dotnet-runtime",
    },
    "native": {
        "url": "https://ziglang.org/download/0.17.0/zig-x86_64-windows-0.17.0.zip",
        "sha256": "b5663f69581dcf391293fbf16c06cb80d81d806545ce618b4d0bab7f0eb8c428",
        "archive": "zig-0.17.0.zip", "destination": ".",
    },
}


def install(component):
    if component != "maven" and (os.name != "nt" or platform.machine().lower() not in ("amd64", "x86_64")):
        raise ValueError("Pinned JDK/runtime/Zig archives target Windows x64. Install the locked .NET runtime and a native C compiler for your platform.")
    artifact = ARTIFACTS[component]
    directory = ROOT / ".work/tools"
    directory.mkdir(parents=True, exist_ok=True)
    archive = directory / artifact["archive"]
    if not archive.is_file():
        partial = archive.with_suffix(".download")
        with urllib.request.urlopen(artifact["url"], timeout=60) as response, partial.open("wb") as output:
            while block := response.read(1024 * 1024):
                output.write(block)
        partial.replace(archive)
    with archive.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != artifact["sha256"]:
        raise ValueError(f"Archive hash mismatch: {archive.name}")
    destination = (directory / artifact["destination"]).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as package:
        for item in package.infolist():
            target = (destination / item.filename).resolve()
            if not target.is_relative_to(destination) or ((item.external_attr >> 16) & 0o170000) == 0o120000:
                raise ValueError("Unsafe archive member")
        package.extractall(destination)
    return {"component": component, **artifact, "installed_at": str(destination)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--component", choices=[*ARTIFACTS, "all"], default="all")
    arguments = parser.parse_args()
    try:
        print(json.dumps([install(name) for name in (ARTIFACTS if arguments.component == "all" else [arguments.component])], indent=2))
    except (OSError, ValueError) as error:
        sys.exit(str(error))
