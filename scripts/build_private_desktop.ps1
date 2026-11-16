param(
    [Parameter(Mandatory=$true)][string]$EnrollmentFile,
    [string]$OutDir = 'artifacts',
    [string]$Uv = 'uv',
    [string]$Cargo = 'cargo'
)
$ErrorActionPreference = 'Stop'
$managerRoot = Split-Path $PSScriptRoot -Parent
$profilePath = (Resolve-Path -LiteralPath $EnrollmentFile).Path
$outputPath = [System.IO.Path]::GetFullPath($OutDir)
Push-Location $managerRoot
try {
    & $Uv run python scripts/build_desktop.py --out $outputPath --enrollment-file $profilePath --uv $Uv --cargo $Cargo
    if ($LASTEXITCODE -ne 0) { throw 'Private desktop build failed' }
} finally {
    Pop-Location
}
