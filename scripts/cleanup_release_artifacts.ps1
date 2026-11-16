param([Parameter(Mandatory=$true)][string]$ReleaseName)
$ErrorActionPreference = 'Stop'
$artifactRoot = [IO.Path]::GetFullPath((Join-Path (Split-Path $PSScriptRoot -Parent) 'artifacts'))
$latest = [IO.Path]::GetFullPath((Join-Path $artifactRoot $ReleaseName))
function Confirm-ArtifactChild([string]$Path) {
    $absolute = [IO.Path]::GetFullPath($Path)
    if (-not $absolute.StartsWith($artifactRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing path outside artifact directory: $absolute"
    }
    $item = Get-Item -LiteralPath $absolute -Force
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Refusing reparse point: $absolute" }
    if ($item.PSIsContainer -and @(Get-ChildItem -LiteralPath $absolute -Recurse -Force -Attributes ReparsePoint).Count) {
        throw "Refusing directory containing reparse points: $absolute"
    }
    return $absolute
}
$null = Confirm-ArtifactChild $latest
foreach ($line in Get-Content -LiteralPath (Join-Path $latest 'ARTIFACTS.sha256')) {
    $parts = $line -split '  ',2
    $file = Confirm-ArtifactChild (Join-Path $latest $parts[1])
    if ((Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLowerInvariant() -ne $parts[0]) {
        throw "Latest release hash mismatch: $($parts[1])"
    }
}
# Keep canonical entry points usable without deleting or restarting managed services.
foreach ($name in @('agent-nexus.exe','agent-nexus-private.exe')) {
    $destination = Join-Path $artifactRoot $name
    $same = (Test-Path -LiteralPath $destination) -and ((Get-FileHash -LiteralPath (Join-Path $latest $name)).Hash -eq (Get-FileHash -LiteralPath $destination).Hash)
    if (-not $same) { Copy-Item -LiteralPath (Join-Path $latest $name) -Destination $destination -Force }
    if ((Get-FileHash -LiteralPath (Join-Path $latest $name)).Hash -ne (Get-FileHash -LiteralPath (Join-Path $artifactRoot $name)).Hash) {
        throw "Canonical executable mismatch: $name"
    }
}
$keep = @($ReleaseName,'research','uv-linux-amd64','agent-nexus.exe','agent-nexus-private.exe','LATEST.json')
$removed = @()
foreach ($entry in Get-ChildItem -LiteralPath $artifactRoot -Force) {
    if ($entry.Name -in $keep) { continue }
    $path = Confirm-ArtifactChild $entry.FullName
    $bytes = if ($entry.PSIsContainer) { (Get-ChildItem -LiteralPath $path -File -Recurse -Force | Measure-Object Length -Sum).Sum } else { $entry.Length }
    $removed += [pscustomobject]@{path=$entry.Name;bytes=$bytes}
    if ($entry.PSIsContainer) { Remove-Item -LiteralPath $path -Recurse -Force } else { Remove-Item -LiteralPath $path -Force }
}
$pointer = [ordered]@{release=$ReleaseName;path=$latest;server_package=(Join-Path $latest 'agent-nexus-server.zip');server_sha256=(Get-FileHash -LiteralPath (Join-Path $latest 'agent-nexus-server.zip')).Hash.ToLowerInvariant()}
$pointer | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $artifactRoot 'LATEST.json') -Encoding utf8
$report = [ordered]@{release=$ReleaseName;removed=$removed;removed_bytes=($removed|Measure-Object bytes -Sum).Sum;preserved_research=$true;preserved_build_input='uv-linux-amd64';canonical_executables_verified=$true}
$report | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $latest 'CLEANUP-REPORT.json') -Encoding utf8
$report | ConvertTo-Json -Depth 5 -Compress
