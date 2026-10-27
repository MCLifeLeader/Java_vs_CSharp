"""Measured deployment bytes and dependency/license metadata; no size estimates."""
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
import zipfile


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def tree_bytes(directory):
    return sum(path.stat().st_size for path in Path(directory).rglob("*") if path.is_file())


def licenses(path):
    if not path or not path.is_file():
        return []
    result = []
    for node in ET.parse(path).getroot().iter():
        if node.tag.rsplit("}", 1)[-1] == "license":
            if list(node):
                result.append({child.tag.rsplit("}", 1)[-1]: child.text for child in node})
            elif node.text:
                result.append({"type": node.attrib.get("type"), "value": node.text})
    return result


def dotnet_inventory(root):
    path = root / "dotnet/Suite/obj/project.assets.json"
    assets = json.loads(path.read_text(encoding="utf-8"))
    folders = [Path(folder) for folder in assets["packageFolders"]]
    components = []
    for identity, item in assets["libraries"].items():
        if item["type"] != "package":
            continue
        name, version = identity.rsplit("/", 1)
        package = next((folder / item["path"] for folder in folders if (folder / item["path"]).is_dir()), None)
        nuspec = next(package.glob("*.nuspec"), None) if package else None
        components.append({"name": name, "version": version, "purl": f"pkg:nuget/{name}@{version}",
                           "content_hash_sha512": item.get("sha512"), "licenses": licenses(nuspec)})
    return components, {"path": "dotnet/Suite/obj/project.assets.json", "sha256": digest(path)}


def java_inventory(artifact_directory):
    jars = list(Path(artifact_directory).rglob("suite.jar"))
    if len(jars) != 1:
        raise ValueError("Expected exactly one packaged suite.jar")
    components = {}
    cache = Path(os.environ.get("MAVEN_USER_HOME", str(Path.home() / ".m2"))) / "repository"
    cached_artifacts = None

    def inspect(payload, artifact_name=None, artifact_sha=None):
        nonlocal cached_artifacts
        identities = []
        with zipfile.ZipFile(payload) as archive:
            for name in archive.namelist():
                if name.startswith("BOOT-INF/lib/") and name.endswith(".jar"):
                    content = archive.read(name)
                    inspect(io.BytesIO(content), Path(name).name, hashlib.sha256(content).hexdigest())
                if name.startswith("META-INF/maven/") and name.endswith("/pom.properties"):
                    values = dict(line.split("=", 1) for line in archive.read(name).decode("utf-8").splitlines() if "=" in line and not line.startswith("#"))
                    if not all(key in values for key in ("groupId", "artifactId", "version")):
                        continue
                    group, artifact, version = (values[key] for key in ("groupId", "artifactId", "version"))
                    identities.append((group, artifact, version))
                    directory = cache / group.replace(".", "/") / artifact / version
                    jar, pom = directory / f"{artifact}-{version}.jar", directory / f"{artifact}-{version}.pom"
                    components[(group, artifact, version)] = {"group": group, "name": artifact, "version": version,
                        "purl": f"pkg:maven/{group}/{artifact}@{version}", "sha256": digest(jar) if jar.is_file() else None,
                        "licenses": licenses(pom)}
        if artifact_name and not identities:
            # Some vendor jars omit Maven metadata. Keep their exact bytes and
            # explicit unknown metadata rather than silently losing a dependency.
            if cached_artifacts is None:
                cached_artifacts = {}
                for cached in cache.rglob("*.jar"):
                    cached_artifacts.setdefault(cached.name, []).append(cached)
            candidates = cached_artifacts.get(artifact_name, [])
            package = candidates[0] if len(candidates) == 1 else None
            if package:
                version, artifact = package.parent.name, package.parent.parent.name
                group = ".".join(package.parent.parent.parent.relative_to(cache).parts)
                components[(group, artifact, version)] = {"group": group, "name": artifact, "version": version,
                    "purl": f"pkg:maven/{group}/{artifact}@{version}", "sha256": artifact_sha,
                    "licenses": licenses(package.with_suffix(".pom"))}
            else:
                components[("unresolved", artifact_name, "unknown")] = {"name": artifact_name, "version": None,
                    "sha256": artifact_sha, "licenses": [], "metadata_status": "Maven coordinates unavailable"}
    inspect(jars[0])
    return list(components.values()), {"path": jars[0].name, "sha256": digest(jars[0])}


def runtime_inventory(root, language):
    if language == "csharp":
        local = root / ".work/tools/dotnet-runtime" / ("dotnet.exe" if os.name == "nt" else "dotnet")
        host = local if local.is_file() else Path(shutil.which("dotnet") or "dotnet")
        output = subprocess.check_output([str(host), "--list-runtimes"], text=True, timeout=20)
        directories = []
        for framework in ("Microsoft.NETCore.App", "Microsoft.AspNetCore.App"):
            match = re.search(r"^" + re.escape(framework) + r" 10\.0\.14 \[(.+)\]$", output, re.MULTILINE)
            if not match:
                raise ValueError(f"Deployment runtime missing: {framework}10.0.14")
            directories.append(Path(match.group(1)) / "10.0.14")
        base = directories[0].parents[1]
        extra = [path for path in base.iterdir() if path.is_file()]
        hostfxr = base / "host/fxr/10.0.14"
        size = sum(tree_bytes(path) for path in directories) + sum(path.stat().st_size for path in extra)
        if hostfxr.is_dir():
            size += tree_bytes(hostfxr)
        return {"bytes": size, "identity": output.strip().splitlines(),
                "boundary": "Required .NETCore+ASP.NETCore10.0.14 trees, root files and hostfxr; SDK and other runtimes excluded",
                "license_files": [path.name for path in extra if "license" in path.name.lower() or "notice" in path.name.lower()]}
    result = subprocess.run(["java", "-XshowSettings:properties", "-version"], capture_output=True, text=True, timeout=20)
    match = re.search(r"^\s*java.home = (.+)$", result.stderr, re.MULTILINE)
    if result.returncode or not match:
        raise ValueError("Cannot inspect active Java runtime")
    home = Path(match.group(1).strip())
    return {"bytes": tree_bytes(home), "identity": [line for line in result.stderr.splitlines() if "version" in line or "Runtime Environment" in line],
            "boundary": "Complete installed JDK distribution including compiler/tools; untrimmed deployable runtime, no jlink optimization",
            "license_files": [str(path.relative_to(home)) for path in home.rglob("*") if path.is_file() and path.name.lower() in ("license", "license.txt", "notice", "notice.txt")]}


def deployment_inventory(root, language, artifact_directory, runtime_root=None):
    root, artifact_directory = Path(root), Path(artifact_directory)
    components, evidence = dotnet_inventory(root) if language == "csharp" else java_inventory(artifact_directory)
    runtime = runtime_inventory(Path(runtime_root) if runtime_root else root, language)
    artifacts = {str(path.relative_to(artifact_directory)): {"bytes": path.stat().st_size, "sha256": digest(path)}
                 for path in artifact_directory.rglob("*") if path.is_file()}
    application_bytes = sum(item["bytes"] for item in artifacts.values())
    return {"sbom": {"format": "benchmark-dependency-inventory-1", "components": components, "evidence": evidence,
                    "license_completeness": "Direct package metadata; missing/inherited licenses remain explicit review gaps"},
            "artifacts": artifacts, "application_bytes": application_bytes, "runtime": runtime,
            "runtime_inclusive_bytes": application_bytes + runtime["bytes"],
            "comparison_policy": "Framework-dependent deployments with disclosed runtime distribution boundaries; trimming/AOT/container layers are separate profiles"}
