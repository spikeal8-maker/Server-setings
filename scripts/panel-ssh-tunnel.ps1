param(
    [Parameter(Mandatory=$true)][string]$ConnectionFile
)
$ErrorActionPreference = 'Stop'
$c = Get-Content -LiteralPath $ConnectionFile -Raw | ConvertFrom-Json
if ($c.host -notmatch '^[a-zA-Z0-9.-]+$' -or $c.user -notmatch '^[a-zA-Z0-9_-]+$') { throw 'Invalid SSH host/user' }
if ([int]$c.local_port -lt 1024 -or [int]$c.local_port -gt 65535) { throw 'Invalid local port' }
if (!(Test-Path -LiteralPath $c.key_file) -or !(Test-Path -LiteralPath $c.known_hosts)) { throw 'Missing private enrollment files' }
# Foreground SSH process: run this script hidden from the desktop shortcut.
# No password, token or security-entrance path is passed to SSH.
& "$env:WINDIR\System32\OpenSSH\ssh.exe" -N -T `
    -i $c.key_file -o "UserKnownHostsFile=$($c.known_hosts)" `
    -o StrictHostKeyChecking=yes -o IdentitiesOnly=yes -o BatchMode=yes `
    -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 `
    -L "127.0.0.1:$($c.local_port):127.0.0.1:19192" "$($c.user)@$($c.host)"
exit $LASTEXITCODE
