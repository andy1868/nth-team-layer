param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $VitestArgs
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$vitest = Join-Path $root "node_modules\vitest\vitest.mjs"

if (-not (Test-Path $vitest)) {
    throw "Vitest entrypoint not found at $vitest. Run npm install first."
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

if (-not $VitestArgs -or $VitestArgs.Count -eq 0) {
    $VitestArgs = @("run", "--environment", "jsdom")
}

& $node $vitest @VitestArgs
exit $LASTEXITCODE
