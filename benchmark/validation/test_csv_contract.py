"""Exercise the production C# Csv class directly through a temporary compiled probe."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[1]


class CsvContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assembly = ROOT / "dotnet/Suite/bin/Release/net10.0/Suite.dll"
        if not assembly.is_file():
            raise RuntimeError("Build the production .NET Suite before CSV verification")
        (ROOT / ".work").mkdir(exist_ok=True)
        cls.temporary = tempfile.TemporaryDirectory(prefix="csv-contract-", dir=ROOT / ".work")
        cls.addClassCleanup(cls.temporary.cleanup)
        directory = Path(cls.temporary.name)
        (directory / "global.json").write_bytes((ROOT / "dotnet/global.json").read_bytes())
        (directory / "CsvProbe.csproj").write_text(
            '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><OutputType>Exe</OutputType><TargetFramework>net10.0</TargetFramework>'
            '<ImplicitUsings>enable</ImplicitUsings><RuntimeFrameworkVersion>10.0.14</RuntimeFrameworkVersion><RollForward>Disable</RollForward>'
            '</PropertyGroup><ItemGroup><Reference Include="Suite"><HintPath>' + escape(str(assembly)) +
            '</HintPath></Reference></ItemGroup></Project>', encoding="utf-8")
        (directory / "Program.cs").write_text(
            'using System.Text.Json; using BenchmarkSuite; '
            'try { var rows=Csv.Parse(JsonSerializer.Deserialize<string>(Console.In.ReadToEnd())!); '
            'Console.WriteLine(JsonSerializer.Serialize(new {status="ok",rows})); } '
            'catch(InvalidDataException) { Console.WriteLine(JsonSerializer.Serialize(new {status="error"})); }', encoding="utf-8")
        build = subprocess.run(["dotnet", "build", str(directory / "CsvProbe.csproj"), "-c", "Release", "--nologo",
                                "--disable-build-servers", "-p:UseSharedCompilation=false", "-nr:false"],
                               cwd=ROOT / "dotnet", text=True, capture_output=True, timeout=90)
        if build.returncode:
            raise RuntimeError(build.stdout + build.stderr)
        cls.probe = directory / "bin/Release/net10.0/CsvProbe.dll"

    def parse(self, source):
        completed = subprocess.run(["dotnet", str(self.probe)], input=json.dumps(source), text=True,
                                   capture_output=True, timeout=10)
        self.assertEqual(0, completed.returncode, completed.stderr)
        return json.loads(completed.stdout)

    def test_quoted_commas_newlines_and_escaped_quotes_are_preserved(self):
        result = self.parse('"a,b","line1\r\nline2","said ""yes""",,last\r\n')
        self.assertEqual({"status": "ok", "rows": [["a,b", "line1\r\nline2", 'said "yes"', "", "last"]]}, result)

    def test_quote_placement_and_unterminated_fields_are_rejected(self):
        for source in ('ab"c",b,c,d,e\r\n', '"a"tail,b,c,d,e\r\n', '"a" ,b,c,d,e\r\n', '"a,b,c,d,e'):
            with self.subTest(source=source):
                self.assertEqual({"status": "error"}, self.parse(source))

    def test_empty_trailing_fields_and_field_count(self):
        self.assertEqual({"status": "ok", "rows": [["", "", "", "", ""]]}, self.parse('"",,,,""'))
        self.assertEqual({"status": "error"}, self.parse('""'))
        self.assertEqual({"status": "error"}, self.parse('a,b,c,d,e,f\r\n'))


if __name__ == "__main__":
    unittest.main()
