param(
    [Parameter(Mandatory=$true)][string]$ConnectionFile,
    [switch]$OpenPanel
)
$ErrorActionPreference = 'Stop'
$c = Get-Content -LiteralPath $ConnectionFile -Raw | ConvertFrom-Json
if ($c.host -notmatch '^[a-zA-Z0-9.-]+$' -or $c.user -notmatch '^[a-zA-Z0-9_-]+$') { throw 'Invalid SSH host/user' }
if ([int]$c.local_port -lt 1024 -or [int]$c.local_port -gt 65535) { throw 'Invalid local port' }
if (!(Test-Path -LiteralPath $c.key_file) -or !(Test-Path -LiteralPath $c.known_hosts)) { throw 'Missing private enrollment files' }
if ($OpenPanel) {
    if ($c.entry_path -notmatch '^/[a-zA-Z0-9_/-]+$') { throw 'Invalid private panel entrance' }
    $listener = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $c.local_port -State Listen -ErrorAction SilentlyContinue
    if ($listener) {
        $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)"
        if ($proc.Name -ne 'ssh.exe' -or !$proc.CommandLine.Contains([string]$c.key_file)) { throw 'Local port is occupied by another application' }
    } else {
        if ($PSCommandPath.Contains('"') -or $ConnectionFile.Contains('"')) { throw 'Invalid launcher path' }
        $child = Start-Process powershell.exe -WindowStyle Hidden -PassThru -ArgumentList @('-NoProfile','-ExecutionPolicy','RemoteSigned','-File',('"'+$PSCommandPath+'"'),'-ConnectionFile',('"'+$ConnectionFile+'"'))
        for ($i=0; $i -lt 40; $i++) {
            Start-Sleep -Milliseconds 250
            $child.Refresh()
            if ($child.HasExited) { throw 'SSH tunnel failed; inspect pinned host/key or network' }
            $listener = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $c.local_port -State Listen -ErrorAction SilentlyContinue
            if ($listener) { break }
        }
        if (!$listener) { throw 'SSH tunnel did not become ready' }
    }
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)"
    if ($proc.Name -ne 'ssh.exe' -or !$proc.CommandLine.Contains([string]$c.key_file)) { throw 'Unexpected owner of local tunnel port' }
    # Keep the private entrance out of process arguments and repository files.
    $shortcut = Join-Path (Split-Path -Parent $ConnectionFile) 'Panel.url'
    Set-Content -LiteralPath $shortcut -Encoding ascii -Value "[InternetShortcut]`r`nURL=http://127.0.0.1:$($c.local_port)$($c.entry_path)"
    Start-Process -FilePath $shortcut
    return
}
# Foreground SSH process: run this script hidden from the desktop shortcut.
# No password, token or security-entrance path is passed to SSH.
& "$env:WINDIR\System32\OpenSSH\ssh.exe" -N -T `
    -i $c.key_file -o "UserKnownHostsFile=$($c.known_hosts)" `
    -o StrictHostKeyChecking=yes -o IdentitiesOnly=yes -o BatchMode=yes `
    -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 `
    -L "127.0.0.1:$($c.local_port):127.0.0.1:19192" "$($c.user)@$($c.host)"
exit $LASTEXITCODE
