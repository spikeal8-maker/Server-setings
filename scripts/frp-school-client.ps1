param(
  [ValidateSet('Prepare','Install','Start','Status','Stop')][string]$Mode = 'Prepare',
  [string]$Bundle,
  [string]$Root = "$env:LOCALAPPDATA\ASA-Lab\frp-school-test"
)
$ErrorActionPreference = 'Stop'
$openssl = 'C:\Program Files\Git\usr\bin\openssl.exe'
$startup = Join-Path ([Environment]::GetFolderPath('Startup')) 'ASA FRP School Test.lnk'
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
loginFailExit = false
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
  # The school operator is non-admin. Use its own logon startup, never an elevated task.
  $shell = New-Object -ComObject WScript.Shell
  $shortcut = $shell.CreateShortcut($startup)
  $target = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
  $arguments = '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $PSCommandPath + '" -Mode Start -Root "' + $Root + '"'
  if ((Test-Path $startup) -and ($shortcut.TargetPath -ne $target -or $shortcut.Arguments -ne $arguments)) { throw 'Startup entry belongs to another deployment' }
  $shortcut.TargetPath = $target; $shortcut.Arguments = $arguments; $shortcut.WindowStyle = 7; $shortcut.Save()
  $running = $false
  if (Test-Path "$Root\process.json") {
    $record = Get-Content "$Root\process.json" -Raw | ConvertFrom-Json
    $process = Get-Process -Id $record.pid -ErrorAction SilentlyContinue
    $running = $process -and $process.Path -eq "$Root\frpc.exe" -and $process.StartTime.ToUniversalTime().Ticks -eq $record.startTicks
  }
  if (!$running) {
    $process = Start-Process -FilePath "$Root\frpc.exe" -ArgumentList ('-c "' + $Root + '\frpc.toml"') -WorkingDirectory $Root -WindowStyle Hidden -PassThru
    @{pid=$process.Id; startTicks=$process.StartTime.ToUniversalTime().Ticks} | ConvertTo-Json | Set-Content "$Root\process.json"
  }
}
if ($Mode -eq 'Stop') {
  if (Test-Path "$Root\process.json") {
    $record = Get-Content "$Root\process.json" -Raw | ConvertFrom-Json
    $process = Get-Process -Id $record.pid -ErrorAction SilentlyContinue
    if ($process -and $process.Path -eq "$Root\frpc.exe" -and $process.StartTime.ToUniversalTime().Ticks -eq $record.startTicks) { Stop-Process -Id $process.Id }
  }
}
if (Test-Path "$Root\process.json") {
  $record = Get-Content "$Root\process.json" -Raw | ConvertFrom-Json
  Get-Process -Id $record.pid -ErrorAction SilentlyContinue | Select-Object Id,ProcessName,StartTime
}
