# PC side of `jy render --remote HOST` (Windows 剪映, same native bridge as the Mac).
# Copied into the job folder by jy for every run; never installed, never scheduled.
#
#   -Mode probe   game-mode flag, 剪映 app dir, bridge state, login, which Mac resources
#                 already exist on this PC (reads probe.json from the job folder)
#   -Mode export  unpacks pkg.tar, POSTs /custom-agent/film-export, moves the mp4 to
#                 its output path, returns the mp4's moov box for verification
#
# Output: exactly one line `JYREMOTE {json}`. The bridge token and the account
# context (cookie, accountScope) never leave this script.
param(
  [Parameter(Mandatory)][ValidateSet('probe', 'export')][string]$Mode,
  [Parameter(Mandatory)][string]$Job,
  [switch]$Launch,
  [int]$LaunchWaitS = 90
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$ProgressPreference = 'SilentlyContinue'

function Emit($o) { Write-Output ('JYREMOTE ' + ($o | ConvertTo-Json -Compress -Depth 8)) }
function Fwd([string]$p) { $p -replace '\\', '/' }

if ($Job -notmatch '^[A-Za-z0-9_-]+$') { Emit @{ ok = $false; error = 'bad_job_id' }; exit 2 }
$userHome = $env:USERPROFILE
$jobDir = Join-Path $userHome ('studio\jobs\jianying\' + $Job)
$flag = Join-Path $userHome 'pcwork\game-mode.on'
$descPath = Join-Path $env:TEMP 'infinite-canvas-native-bridge.json'
$cacheRoot = Join-Path $env:LOCALAPPDATA 'JianyingPro\User Data\Cache'
$appDir = Get-ChildItem (Join-Path $env:LOCALAPPDATA 'JianyingPro\Apps') -Directory -EA 0 |
  Where-Object { $_.Name -match '^\d+(\.\d+)+$' -and (Test-Path (Join-Path $_.FullName 'JianyingPro.exe')) } |
  Sort-Object { [version]$_.Name } -Descending | Select-Object -First 1
$script:Desc = $null

function GameMode { if (Test-Path $flag) { $t = Get-Content $flag -Raw -EA 0; if (-not $t) { $t = 'on' }; return $t.Trim() } return $null }

function Http([string]$method, [string]$url, [string]$token, $body, [int]$timeoutMs) {
  $req = [Net.HttpWebRequest]::Create($url)
  $req.Method = $method; $req.Proxy = $null
  $req.Timeout = $timeoutMs; $req.ReadWriteTimeout = $timeoutMs
  $req.Headers.Add('x-infinite-canvas-native-token', $token)
  try {
    if ($null -ne $body) {
      $bytes = [Text.Encoding]::UTF8.GetBytes([string]$body)
      $req.ContentType = 'application/json; charset=utf-8'
      $req.ContentLength = $bytes.Length
      $s = $req.GetRequestStream(); $s.Write($bytes, 0, $bytes.Length); $s.Close()
    }
    $resp = $req.GetResponse()
  } catch {
    $ex = $_.Exception
    while ($ex -and -not ($ex -is [Net.WebException])) { $ex = $ex.InnerException }
    if (-not $ex -or -not $ex.Response) {
      $kind = 'transport'
      if ($ex -and $ex.Status -eq [Net.WebExceptionStatus]::Timeout) { $kind = 'timeout' }
      return @{ status = 0; kind = $kind; body = ''; error = $_.Exception.Message }
    }
    $resp = $ex.Response
  }
  $sr = New-Object IO.StreamReader($resp.GetResponseStream(), [Text.Encoding]::UTF8)
  $text = $sr.ReadToEnd(); $sr.Close()
  $code = [int]$resp.StatusCode; $resp.Close()
  return @{ status = $code; kind = 'http'; body = $text }
}

function Bridge {
  $r = @{ state = 'not_running'; pid = $null; port = $null; health_ms = $null; uptime_s = $null; error = $null }
  if (-not (Test-Path $descPath)) { $r.error = 'no descriptor'; return $r }
  try { $d = Get-Content $descPath -Raw -Encoding UTF8 | ConvertFrom-Json } catch { $r.error = 'descriptor unreadable'; return $r }
  $r.pid = $d.pid; $r.port = $d.port
  $p = $null; if ($d.pid) { $p = Get-Process -Id $d.pid -EA 0 }
  if (-not $p) { $r.error = 'descriptor pid not alive'; return $r }
  try { $r.uptime_s = [int]((Get-Date) - $p.StartTime).TotalSeconds } catch {}
  $sw = [Diagnostics.Stopwatch]::StartNew()
  $h = Http 'GET' "http://127.0.0.1:$($d.port)/.infinite-canvas-native-bridge/health" $d.token $null 5000
  $r.health_ms = [int]$sw.ElapsedMilliseconds
  if ($h.status -eq 200 -and $h.body -match '"ok"\s*:\s*true') { $r.state = 'ok'; $script:Desc = $d }
  else { $r.state = 'wedged'; $r.error = "health status=$($h.status) kind=$($h.kind)" }
  return $r
}

function Scope {
  $c = Http 'GET' "http://127.0.0.1:$($script:Desc.port)/custom-agent/context" $script:Desc.token $null 120000
  if ($c.status -ne 200) { return @{ ok = $false; status = $c.status; kind = $c.kind } }
  $pr = ($c.body | ConvertFrom-Json).authContext.principal
  $s = [string]$pr.accountScope
  if (-not $s -and $pr.userId) { $s = 'uid:' + $pr.userId }
  return @{ ok = [bool]$s; scope = $s; status = 200 }
}

function ReadBoxes([string]$path) {
  # Top-level MP4 boxes + the moov box (base64) so the Mac can verify the file.
  $res = @{ boxes = @(); moov_b64 = $null }
  $fs = [IO.File]::OpenRead($path)
  try {
    $len = $fs.Length; $pos = [int64]0; $hdr = New-Object byte[] 16
    while ($pos + 8 -le $len -and $res.boxes.Count -lt 64) {
      $fs.Position = $pos; [void]$fs.Read($hdr, 0, 16)
      $size = [int64]$hdr[0] * 16777216 + [int64]$hdr[1] * 65536 + [int64]$hdr[2] * 256 + [int64]$hdr[3]
      $type = [Text.Encoding]::ASCII.GetString($hdr, 4, 4)
      $head = 8
      if ($size -eq 1) {
        $size = [int64]0
        for ($i = 8; $i -lt 16; $i++) { $size = ($size * 256) + $hdr[$i] }
        $head = 16
      } elseif ($size -eq 0) { $size = $len - $pos }
      if ($size -lt $head) { break }
      $res.boxes += $type
      if ($type -eq 'moov' -and $size -le 16MB) {
        $buf = New-Object byte[] ([int]$size); $fs.Position = $pos; [void]$fs.Read($buf, 0, [int]$size)
        $res.moov_b64 = [Convert]::ToBase64String($buf)
      }
      $pos += $size
    }
  } finally { $fs.Close() }
  return $res
}

function Ready {
  $b = Bridge
  $out = @{ bridge = $b; logged_in = $null }
  if ($b.state -eq 'ok') { $s = Scope; $out.logged_in = $s.ok; $out.context_status = $s.status }
  return $out
}

$gm = GameMode
if ($gm) { Emit @{ ok = $false; error = 'game_mode'; game_mode = $gm }; exit 4 }
if (-not $appDir) { Emit @{ ok = $false; error = 'jianying_not_installed' }; exit 3 }
$appRes = Join-Path $appDir.FullName 'Resources'

if ($Mode -eq 'probe') {
  $in = Get-Content (Join-Path $jobDir 'probe.json') -Raw -Encoding UTF8 | ConvertFrom-Json
  $state = Ready
  $launched = $null
  if ($state.bridge.state -eq 'not_running' -and $Launch) {
    $gui = Join-Path $userHome 'pcwork\run-gui.ps1'
    if (-not (Test-Path $gui)) { Emit @{ ok = $false; error = 'run_gui_missing' }; exit 3 }
    $sw = [Diagnostics.Stopwatch]::StartNew()
    & $gui -Exe (Join-Path $appDir.FullName 'JianyingPro.exe') | Out-Null
    do { Start-Sleep 2; $state = Ready } while ($state.bridge.state -ne 'ok' -and $sw.Elapsed.TotalSeconds -lt $LaunchWaitS)
    $launched = @{ waited_ms = [int]$sw.ElapsedMilliseconds; bridge_back = ($state.bridge.state -eq 'ok') }
  }
  $appExists = @{}; $fontAlt = @{}
  foreach ($rel in @($in.app_rels)) {
    if (-not $rel) { continue }
    $p = Join-Path $appRes $rel
    $appExists[$rel] = (Test-Path -LiteralPath $p)
    if (-not $appExists[$rel] -and $rel -like 'Font/*') {
      $hit = Get-ChildItem (Join-Path $appRes 'Font') -Recurse -File -Filter (Split-Path $rel -Leaf) -EA 0 | Select-Object -First 1
      if ($hit) { $fontAlt[$rel] = Fwd $hit.FullName }
    }
  }
  $cacheExists = @{}
  foreach ($rel in @($in.cache_rels)) { if ($rel) { $cacheExists[$rel] = (Test-Path -LiteralPath (Join-Path $cacheRoot $rel)) } }
  $packages = @{}
  foreach ($key in @($in.package_keys)) {
    if (-not $key) { continue }
    $packages[$key] = @(Get-ChildItem -LiteralPath (Join-Path $cacheRoot $key) -Directory -EA 0 | Select-Object -Expand Name)
  }
  $outExists = $false
  if ($in.output_rel) { $outExists = Test-Path -LiteralPath (Join-Path $userHome $in.output_rel) }
  Emit @{
    ok = $true; mode = 'probe'; app_version = $appDir.Name
    app_resources = (Fwd $appRes); cache_root = (Fwd $cacheRoot); job_dir = (Fwd $jobDir); home = (Fwd $userHome)
    bridge = $state.bridge; logged_in = $state.logged_in; context_status = $state.context_status; launched = $launched
    app_exists = $appExists; font_alt = $fontAlt; cache_exists = $cacheExists; packages = $packages
    output_exists = $outExists
  }
  exit 0
}

# ---- export
$in = Get-Content (Join-Path $jobDir 'export.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$tarPath = Join-Path $jobDir 'pkg.tar'
if (Test-Path $tarPath) {
  & tar.exe -xf $tarPath -C $jobDir 2>&1 | Out-Null
  if ($LASTEXITCODE -ne 0) { Emit @{ ok = $false; error = 'unpack_failed'; exit_code = $LASTEXITCODE }; exit 3 }
  Remove-Item -LiteralPath $tarPath -Force
}
# Effect packages the PC's 剪映 cache lacks: move them in (never overwrite). 剪映
# ignores a package anywhere else, the same way it does on the Mac.
$installed = @()
foreach ($rel in @($in.installs)) {
  if (-not $rel -or $rel -notmatch '^(effect|artistEffect)/\d+/[0-9a-f]+$') { continue }
  $src = Join-Path $jobDir ('install\' + ($rel -replace '/', '\'))
  $dst = Join-Path $cacheRoot ($rel -replace '/', '\')
  if ((Test-Path -LiteralPath $src) -and -not (Test-Path -LiteralPath $dst)) {
    New-Item -ItemType Directory -Force (Split-Path $dst -Parent) | Out-Null
    Move-Item -LiteralPath $src -Destination $dst
    if (Test-Path -LiteralPath "${src}_modity_time.txt") { Move-Item -LiteralPath "${src}_modity_time.txt" -Destination "${dst}_modity_time.txt" }
    $installed += $rel
  }
}
$draftDir = Join-Path $jobDir 'draft'
$info = Join-Path $draftDir 'draft_info.json'
if (-not (Test-Path -LiteralPath $info)) { Emit @{ ok = $false; error = 'draft_missing' }; exit 3 }
$final = Join-Path $userHome $in.output_rel
if ((Test-Path -LiteralPath $final) -and -not $in.force) { Emit @{ ok = $false; error = 'output_exists'; output_path = (Fwd $final) }; exit 3 }

$state = Ready
if ($state.bridge.state -ne 'ok') { Emit @{ ok = $false; error = 'bridge_' + $state.bridge.state; bridge = $state.bridge }; exit 3 }
$scope = Scope
if (-not $scope.ok) { Emit @{ ok = $false; error = 'not_logged_in'; context_status = $scope.status }; exit 3 }

$tmpOut = Join-Path $jobDir 'out.mp4'
Remove-Item -LiteralPath $tmpOut -Force -EA 0
$sha = (Get-FileHash -LiteralPath $info -Algorithm SHA256).Hash.ToLower()
$id = [guid]::NewGuid().ToString()
$body = @{
  accountScope = $scope.scope; requestId = $id; snapshotId = "film-export-$id"; revision = "sha256:$sha"
  draftPath = $draftDir; draftSha256 = $sha; outputPath = $tmpOut; format = 'mp4'; timeoutMs = [int]$in.timeout_ms
} | ConvertTo-Json -Compress
$scope = $null
$sw = [Diagnostics.Stopwatch]::StartNew()
$r = Http 'POST' "http://127.0.0.1:$($script:Desc.port)/custom-agent/film-export" $script:Desc.token $body ([int]$in.timeout_ms + 30000)
$elapsed = [int]$sw.ElapsedMilliseconds
$body = $null
$res = @{ ok = $false; mode = 'export'; status = $r.status; kind = $r.kind; body = $r.body; elapsed_ms = $elapsed
  draft_sha256 = $sha; uptime_s = $state.bridge.uptime_s; transport_error = $r.error; installed = $installed }
if ($r.status -eq 200 -and (Test-Path -LiteralPath $tmpOut)) {
  New-Item -ItemType Directory -Force (Split-Path $final -Parent) | Out-Null
  if (Test-Path -LiteralPath $final) { Remove-Item -LiteralPath $final -Force }
  Move-Item -LiteralPath $tmpOut -Destination $final
  $boxes = ReadBoxes $final
  $res.ok = $true; $res.output_path = (Fwd $final); $res.bytes = (Get-Item -LiteralPath $final).Length
  $res.boxes = $boxes.boxes; $res.moov_b64 = $boxes.moov_b64
}
Emit $res
exit 0
