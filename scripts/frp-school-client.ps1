param(
  [ValidateSet('Prepare','Install','Start','Status','Stop')][string]$Mode = 'Prepare',
  [string]$Bundle,
  [string]$Root = "$env:LOCALAPPDATA\ASA-Lab\frp-school-test"
)
$ErrorActionPreference = 'Stop'
$openssl = 'C:\Program Files\Git\usr\bin\openssl.exe'
$taskName = 'ASA-FRP-School-Test'
if ($Mode -eq 'Prepare') {
  if (!(Test-Path -LiteralPath $openssl)) { throw 'Existing Git OpenSSL is required; no Git installer is run' }
  New-Item -ItemType Directory -Force -Path $Root | Out-Null
  if ((Get-Item -LiteralPath $Root).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Root cannot be a reparse point' }
  $identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
  & icacls.exe $Root /inheritance:r /grant:r "${identity}:(OI)(CI)F" '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' | Out-Null
  if ($LASTEXITCODE) { throw 'Private ACL failed' }
  if (!(Test-Path "$Root\client.key")) {
    $proc = Start-Process -FilePath $openssl -ArgumentList @('req','-new','-newkey','rsa:3072','-nodes','-subj','/CN=ASA-school-FRP-test','-keyout',('"'+$Root+'\client.key"'),'-out',('"'+$Root+'\client.csr"')) -Wait -PassThru -WindowStyle Hidden -RedirectStandardError "$Root\keygen.log" -RedirectStandardOutput "$Root\keygen.stdout.log"
    if ($proc.ExitCode) { throw 'CSR generation failed' }
  }
  $zip = "$Root\frp.zip"
  if (!(Test-Path $zip)) { Invoke-WebRequest -UseBasicParsing 'https://github.com/fatedier/frp/releases/download/v0.71.1/frp_0.71.1_windows_amd64.zip' -OutFile $zip }
  if ((Get-FileHash $zip -Algorithm SHA256).Hash.ToLowerInvariant() -ne 'b90074b2b6a1d792a7a40f085ac6d276bcab2d1bc60201f8a86ca53ce65ce821') { throw 'Official FRP download checksum mismatch' }
  Add-Type -AssemblyName System.IO.Compression.FileSystem
  $archive = [IO.Compression.ZipFile]::OpenRead($zip)
  try {
    $entry = $archive.GetEntry('frp_0.71.1_windows_amd64/frpc.exe')
    if (!$entry) { throw 'Expected binary absent' }
    if (!(Test-Path "$Root\frpc.exe")) { [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, "$Root\frpc.exe", $false) }
    $stream = $entry.Open()
    try { $expected = [BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash($stream)).Replace('-','') } finally { $stream.Dispose() }
    if ((Get-FileHash "$Root\frpc.exe" -Algorithm SHA256).Hash -ne $expected) { throw 'Installed FRPC differs from verified archive' }
  } finally { $archive.Dispose() }
  'PUBLIC_CSR_BASE64=' + [Convert]::ToBase64String([IO.File]::ReadAllBytes("$Root\client.csr"))
  return
}
if ($Mode -eq 'Install') {
  Add-Type -AssemblyName System.IO.Compression.FileSystem
  $archive = [IO.Compression.ZipFile]::OpenRead($Bundle)
  try {
    foreach ($name in @('client.crt','ca.crt','token.rsa')) {
      $entry = $archive.GetEntry($name)
      if (!$entry) { throw "Missing public bundle member $name" }
      [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, "$Root\$name", $true)
    }
  } finally { $archive.Dispose() }
  & $openssl pkeyutl -decrypt -inkey "$Root\client.key" -in "$Root\token.rsa" -out "$Root\token" -pkeyopt rsa_padding_mode:oaep -pkeyopt rsa_oaep_md:sha256
  if ($LASTEXITCODE) { throw 'Token decryption failed' }
  $r = $Root.Replace('\','/')
  New-Item -ItemType Directory -Force "$Root\probe" | Out-Null
  if (!(Test-Path "$Root\probe\index.html")) { [IO.File]::WriteAllText("$Root\probe\index.html", ('ASA-FRP-SCHOOL-PROOF-' + [guid]::NewGuid().ToString()), (New-Object Text.UTF8Encoding $false)) }
  $config = @"
serverAddr = "100.75.149.26"
serverPort = 7000
auth.method = "token"
auth.tokenSource.type = "file"
auth.tokenSource.file.path = "$r/token"
transport.tls.enable = true
transport.tls.certFile = "$r/client.crt"
transport.tls.keyFile = "$r/client.key"
transport.tls.trustedCaFile = "$r/ca.crt"
transport.tls.serverName = "test.asa-lab.ru"
log.to = "$r/frpc.log"
log.level = "info"
[[proxies]]
name = "school-asa-test"
type = "tcp"
localIP = "127.0.0.1"
localPort = 4610
remotePort = 14610
[[proxies]]
name = "school-proof-test"
type = "tcp"
remotePort = 14620
plugin.type = "static_file"
plugin.localPath = "$r/probe"
"@
  [IO.File]::WriteAllText("$Root\frpc.toml", $config, (New-Object Text.UTF8Encoding $false))
  & "$Root\frpc.exe" verify -c "$Root\frpc.toml"
  if ($LASTEXITCODE) { throw 'FRPC config verification failed' }
  return
}
if ($Mode -eq 'Start') {
  $existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
  if ($existing) {
    if ($existing.Actions.Execute -ne "$Root\frpc.exe") { throw 'Task belongs to another deployment' }
  } else {
    $action = New-ScheduledTaskAction -Execute "$Root\frpc.exe" -Argument ('-c "' + $Root + '\frpc.toml"') -WorkingDirectory $Root
    $trigger = New-ScheduledTaskTrigger -AtStartup
    # FRPC needs no administrator privileges; do not elevate a user-writable executable to SYSTEM.
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType S4U -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings | Out-Null
  }
  if ((Get-ScheduledTask -TaskName $taskName).State -ne 'Running') { Start-ScheduledTask -TaskName $taskName }
}
if ($Mode -eq 'Stop') {
  $existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
  if ($existing -and $existing.Actions.Execute -eq "$Root\frpc.exe") { Stop-ScheduledTask -TaskName $taskName }
}
Get-ScheduledTask -TaskName $taskName | Select-Object TaskName,State
