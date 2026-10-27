"""Verify packaged third-party runtime JARs against the recorded hash inventory."""
import hashlib,json,sys,zipfile
from pathlib import Path
project=Path(__file__).resolve().parent
expected={row['artifact']:row['sha256'] for row in json.loads((project/'dependencies.lock.json').read_text())['artifacts']}
with zipfile.ZipFile(project/'target/suite.jar') as package:
    actual={Path(name).name:hashlib.sha256(package.read(name)).hexdigest() for name in package.namelist() if name.startswith('BOOT-INF/lib/') and name.endswith('.jar') and '/database-worker-' not in name}
if actual!=expected:
    print(json.dumps({'missing':sorted(expected.keys()-actual.keys()),'extra':sorted(actual.keys()-expected.keys()),'changed':sorted(name for name in actual.keys()&expected.keys() if actual[name]!=expected[name])},indent=2))
    sys.exit(1)
print(f"Verified {len(actual)} third-party runtime JAR SHA-256 values")
