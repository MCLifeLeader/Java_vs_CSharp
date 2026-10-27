# Dot-source this file for the current PowerShell session only.
$benchmarkToolsDirectory = [System.IO.Path]::GetFullPath($PSScriptRoot)
$benchmarkLocalJdk = Join-Path $benchmarkToolsDirectory '../.work/tools/jdk-25.0.3+9'
if (Test-Path -LiteralPath (Join-Path $benchmarkLocalJdk 'bin/java.exe')) {
    $env:JAVA_HOME = [System.IO.Path]::GetFullPath($benchmarkLocalJdk)
    $env:PATH = (Join-Path $env:JAVA_HOME 'bin') + [System.IO.Path]::PathSeparator + $env:PATH
}
