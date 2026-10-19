$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$frontendRoot = Join-Path $repoRoot "frontend"
$backendRoot = Join-Path $repoRoot "backend"
$tempBase = (Resolve-Path $env:TEMP).Path.TrimEnd('\')
$runId = [Guid]::NewGuid().ToString("N")
$dataRoot = Join-Path $tempBase "lynewright-u2-$runId"
$screenshotRoot = Join-Path $frontendRoot ("e2e\screens\u2-" + (Get-Date -Format "yyyyMMdd-HHmmss") + "-" + $runId.Substring(0, 8))
$apiProcess = $null
$webProcess = $null
$environmentNames = @(
  "LYNEWRIGHT_DEMO", "LYNEWRIGHT_PLUGINS", "LYNEWRIGHT_DB_PATH", "LYNEWRIGHT_MANAGED_MEDIA_DIR", "LYNEWRIGHT_PUBLIC_URL",
  "LYNEWRIGHT_RUNTIME_WORKER_ENABLED", "LYNEWRIGHT_RUNTIME_WORKER_MODE", "LYNEWRIGHT_DISABLE_BACKGROUND_SCHEDULERS", "LYNEWRIGHT_OPERATOR_AUTOMATION_ENABLED",
  "LYNEWRIGHT_BOOTSTRAP_EMAIL", "LYNEWRIGHT_BOOTSTRAP_PASSWORD", "LYNEWRIGHT_SCREENSHOT_EMAIL", "LYNEWRIGHT_SCREENSHOT_PASSWORD",
  "LYNEWRIGHT_SCREENSHOT_BASE_URL", "LYNEWRIGHT_SCREENSHOT_DIR", "LYNEWRIGHT_DEV_API_TARGET", "LYNEWRIGHT_E2E_OUTPUT_DIR"
)
$savedEnvironment = @{}
foreach ($name in $environmentNames) { $savedEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, "Process") }

function Get-FreeLoopbackPort {
  $listener = [System.Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
  $listener.Start()
  try { return ([Net.IPEndPoint]$listener.LocalEndpoint).Port }
  finally { $listener.Stop() }
}

function Wait-Http([string] $url, [int] $timeoutSeconds, [string] $label) {
  $deadline = (Get-Date).AddSeconds($timeoutSeconds)
  while ((Get-Date) -lt $deadline) {
    try { $response = Invoke-WebRequest -Uri $url -TimeoutSec 2 -UseBasicParsing; if ($response.StatusCode -eq 200) { return } }
    catch { Start-Sleep -Milliseconds 500 }
  }
  throw "$label did not become ready at $url"
}

try {
  New-Item -ItemType Directory -Path $dataRoot -Force | Out-Null
  New-Item -ItemType Directory -Path $screenshotRoot -Force | Out-Null
  $apiPort = Get-FreeLoopbackPort
  $webPort = Get-FreeLoopbackPort
  $email = "u2-$runId@example.test"
  $passwordBytes = New-Object byte[] 36
  $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
  $rng.GetBytes($passwordBytes)
  $password = [Convert]::ToBase64String($passwordBytes)
  [Array]::Clear($passwordBytes, 0, $passwordBytes.Length)
  $rng.Dispose()

  $env:LYNEWRIGHT_DEMO = "1"
  $env:LYNEWRIGHT_PLUGINS = "app.integrations.demo.bootstrap"
  $env:LYNEWRIGHT_DB_PATH = Join-Path $dataRoot "lynewright.sqlite3"
  $env:LYNEWRIGHT_MANAGED_MEDIA_DIR = Join-Path $dataRoot "managed-media"
  $env:LYNEWRIGHT_PUBLIC_URL = "http://127.0.0.1:$apiPort"
  $env:LYNEWRIGHT_RUNTIME_WORKER_ENABLED = "1"
  $env:LYNEWRIGHT_RUNTIME_WORKER_MODE = "embedded"
  $env:LYNEWRIGHT_DISABLE_BACKGROUND_SCHEDULERS = "1"
  $env:LYNEWRIGHT_OPERATOR_AUTOMATION_ENABLED = "0"
  $env:LYNEWRIGHT_BOOTSTRAP_EMAIL = $email
  $env:LYNEWRIGHT_BOOTSTRAP_PASSWORD = $password
  $env:LYNEWRIGHT_SCREENSHOT_EMAIL = $email
  $env:LYNEWRIGHT_SCREENSHOT_PASSWORD = $password
  $env:LYNEWRIGHT_SCREENSHOT_BASE_URL = "http://127.0.0.1:$webPort"
  $env:LYNEWRIGHT_SCREENSHOT_DIR = $screenshotRoot
  $env:LYNEWRIGHT_DEV_API_TARGET = "http://127.0.0.1:$apiPort"
  $env:LYNEWRIGHT_E2E_OUTPUT_DIR = Join-Path $dataRoot "playwright-results"

  $apiOut = Join-Path $dataRoot "api.stdout.log"; $apiErr = Join-Path $dataRoot "api.stderr.log"
  $webOut = Join-Path $dataRoot "web.stdout.log"; $webErr = Join-Path $dataRoot "web.stderr.log"
  $python = (Get-Command python.exe -ErrorAction Stop).Source
  $npm = (Get-Command npm.cmd -ErrorAction Stop).Source
  $apiProcess = Start-Process -FilePath $python -ArgumentList @("-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "$apiPort") -WorkingDirectory $backendRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput $apiOut -RedirectStandardError $apiErr
  Wait-Http "http://127.0.0.1:$apiPort/health" 90 "Fresh U2 API"
  $apiProcess.Refresh(); if ($apiProcess.HasExited) { throw "The U2 API exited with code $($apiProcess.ExitCode)." }
  $webProcess = Start-Process -FilePath $npm -ArgumentList @("run", "dev", "--", "--port", "$webPort") -WorkingDirectory $frontendRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput $webOut -RedirectStandardError $webErr
  Wait-Http "http://127.0.0.1:$webPort/overview" 60 "U2 dashboard"
  $webProcess.Refresh(); if ($webProcess.HasExited) { throw "The U2 dashboard exited with code $($webProcess.ExitCode)." }

  Push-Location $frontendRoot
  try { & $npm run e2e -- --grep "U2 walkthrough"; if ($LASTEXITCODE -ne 0) { throw "U2 Playwright exited with code $LASTEXITCODE." } }
  finally { Pop-Location }
  Write-Output "U2 screenshots: $screenshotRoot"
  Write-Output "Temporary data directory: removed after this run"
} catch {
  Write-Error $_
  if (Test-Path (Join-Path $dataRoot "api.stdout.log")) { Get-Content (Join-Path $dataRoot "api.stdout.log") -Tail 60 }
  if (Test-Path (Join-Path $dataRoot "api.stderr.log")) { Get-Content (Join-Path $dataRoot "api.stderr.log") -Tail 60 }
  if (Test-Path (Join-Path $dataRoot "web.stdout.log")) { Get-Content (Join-Path $dataRoot "web.stdout.log") -Tail 30 }
  if (Test-Path (Join-Path $dataRoot "web.stderr.log")) { Get-Content (Join-Path $dataRoot "web.stderr.log") -Tail 30 }
  throw
} finally {
  if ($webProcess -and -not $webProcess.HasExited) { & taskkill.exe /PID $webProcess.Id /T /F 2>$null | Out-Null }
  if ($apiProcess -and -not $apiProcess.HasExited) { Stop-Process -Id $apiProcess.Id -Force -ErrorAction SilentlyContinue }
  foreach ($name in $environmentNames) {
    $value = $savedEnvironment[$name]
    if ($null -eq $value) { [Environment]::SetEnvironmentVariable($name, $null, "Process") }
    else { [Environment]::SetEnvironmentVariable($name, $value, "Process") }
  }
  if (Test-Path -LiteralPath $dataRoot) {
    $resolvedData = (Resolve-Path -LiteralPath $dataRoot).Path.TrimEnd('\')
    if (-not $resolvedData.StartsWith($tempBase + '\', [StringComparison]::OrdinalIgnoreCase) -or (Split-Path $resolvedData -Leaf) -ne "lynewright-u2-$runId") { throw "Refusing to remove unexpected temporary path: $resolvedData" }
    Remove-Item -LiteralPath $resolvedData -Recurse -Force
  }
}
