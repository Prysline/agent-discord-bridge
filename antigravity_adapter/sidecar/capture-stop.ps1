[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$RendezvousPath)

$raw = [Console]::In.ReadToEnd()
try {
    if (-not (Test-Path -LiteralPath $RendezvousPath -PathType Leaf)) { throw 'rendezvous_unavailable' }
    $runtime = Get-Content -LiteralPath $RendezvousPath -Raw | ConvertFrom-Json -ErrorAction Stop
    if (
        [string]$runtime.host -ne '127.0.0.1' -or
        [int]$runtime.port -lt 1 -or [int]$runtime.port -gt 65535 -or
        [string]::IsNullOrWhiteSpace([string]$runtime.token) -or
        [string]::IsNullOrWhiteSpace([string]$runtime.instanceId)
    ) { throw 'rendezvous_invalid' }
    $headers = @{
        Authorization = "Bearer $($runtime.token)"
        'X-Sidecar-Instance' = [string]$runtime.instanceId
    }
    Invoke-WebRequest `
        -UseBasicParsing `
        -Method Post `
        -Uri ("http://127.0.0.1:{0}/stop" -f $runtime.port) `
        -Headers $headers `
        -ContentType 'application/json; charset=utf-8' `
        -Body ([Text.UTF8Encoding]::new($false).GetBytes($raw)) `
        -TimeoutSec 1 | Out-Null
}
catch {
    # Stop notification is best-effort; never block Antigravity shutdown.
}
[Console]::Out.Write('{"decision":"stop"}')
