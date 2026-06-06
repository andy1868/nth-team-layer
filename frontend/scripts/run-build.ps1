$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$tsc = Join-Path $root "node_modules\typescript\bin\tsc"
$vite = Join-Path $root "node_modules\vite\bin\vite.js"

foreach ($entry in @($tsc, $vite)) {
    if (-not (Test-Path $entry)) {
        throw "Build entrypoint not found at $entry. Run npm install first."
    }
}

$candidates = @()
if ($env:NTH_DAO_NODE) {
    $candidates += $env:NTH_DAO_NODE
}
$candidates += @(
    "C:\Program Files\nodejs\node.exe",
    "$env:LOCALAPPDATA\Programs\nodejs\node.exe"
)

$pathNode = Get-Command node.exe -All -ErrorAction SilentlyContinue |
    Where-Object { $_.Source -and ($_.Source -notlike "*\WindowsApps\*") } |
    Select-Object -First 1 -ExpandProperty Source
if ($pathNode) {
    $candidates += $pathNode
}

$node = $candidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if (-not $node) {
    throw "Could not find a usable node.exe. Set NTH_DAO_NODE to an absolute node.exe path."
}

& $node $tsc
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

& $node $vite build
exit $LASTEXITCODE
