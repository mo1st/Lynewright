$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$frontendRoot = Join-Path $repoRoot "frontend"
$backendRoot = Join-Path $repoRoot "backend"
$catalogueRoot = Join-Path $repoRoot "catalogue"
$catalogueDist = Join-Path $catalogueRoot "dist"
$tempBase = (Resolve-Path $env:TEMP).Path.TrimEnd('\')
$runId = [Guid]::NewGuid().ToString("N")
$dataRoot = Join-Path $tempBase "lynewright-u1-$runId"
$screenshotRoot = Join-Path $frontendRoot ("e2e\screens\u1-" + (Get-Date -Format "yyyyMMdd-HHmmss") + "-" + $runId.Substring(0, 8))
$apiPort = $null
$webPort = $null
$apiProcess = $null
$webProcess = $null
$catalogueProcess = $null
$environmentNames = @(
  "LYNEWRIGHT_DEMO", "LYNEWRIGHT_PLUGINS", "LYNEWRIGHT_DB_PATH",
  "LYNEWRIGHT_CATALOGUE_URL", "LYNEWRIGHT_CATALOGUE_ALLOW_DEV_KEY", "LYNEWRIGHT_CATALOGUE_INTERNAL_ENDPOINT_REF",
  "LYNEWRIGHT_INTERNAL_ENDPOINT__U1B_CATALOGUE",
  "LYNEWRIGHT_BOOTSTRAP_EMAIL", "LYNEWRIGHT_BOOTSTRAP_PASSWORD",
  "LYNEWRIGHT_SCREENSHOT_EMAIL", "LYNEWRIGHT_SCREENSHOT_PASSWORD",
  "LYNEWRIGHT_SCREENSHOT_BASE_URL", "LYNEWRIGHT_SCREENSHOT_DIR",
  "LYNEWRIGHT_E2E_OUTPUT_DIR", "LYNEWRIGHT_DEV_API_TARGET", "LYNEWRIGHT_E2E_GREP"
)
$savedEnvironment = @{}
foreach ($name in $environmentNames) { $savedEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, "Process") }

function Wait-Http([string] $url, [int] $timeoutSeconds, [string] $label) {
  $deadline = (Get-Date).AddSeconds($timeoutSeconds)
  while ((Get-Date) -lt $deadline) {
    try {
      $response = Invoke-WebRequest -Uri $url -TimeoutSec 2 -UseBasicParsing
      if ($response.StatusCode -eq 200) { return }
    } catch { Start-Sleep -Milliseconds 500 }
  }
  throw "$label did not become ready at $url"
}

function Get-FreeLoopbackPort {
  $listener = [System.Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
  $listener.Start()
  try { return ([Net.IPEndPoint]$listener.LocalEndpoint).Port }
  finally { $listener.Stop() }
}

try {
  New-Item -ItemType Directory -Path $dataRoot -Force | Out-Null
  New-Item -ItemType Directory -Path $screenshotRoot -Force | Out-Null
  if (-not (Test-Path -LiteralPath (Join-Path $catalogueDist "catalogue.json")) -or -not (Test-Path -LiteralPath (Join-Path $catalogueDist "catalogue.json.minisig"))) {
    throw "The serialled and signed development catalogue is required at $catalogueDist."
  }

  $email = "u1-$runId@example.test"
  $passwordBytes = New-Object byte[] 36
  $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
  $rng.GetBytes($passwordBytes)
  $password = [Convert]::ToBase64String($passwordBytes)
  [Array]::Clear($passwordBytes, 0, $passwordBytes.Length)
  $rng.Dispose()

  $env:LYNEWRIGHT_DEMO = "1"
  $env:LYNEWRIGHT_PLUGINS = "app.integrations.demo.bootstrap"
  $env:LYNEWRIGHT_DB_PATH = Join-Path $dataRoot "lynewright.sqlite3"
  $env:LYNEWRIGHT_BOOTSTRAP_EMAIL = $email
  $env:LYNEWRIGHT_BOOTSTRAP_PASSWORD = $password
  $env:LYNEWRIGHT_SCREENSHOT_EMAIL = $email
  $env:LYNEWRIGHT_SCREENSHOT_PASSWORD = $password
  $env:LYNEWRIGHT_SCREENSHOT_BASE_URL = "http://127.0.0.1:$webPort"
  $env:LYNEWRIGHT_SCREENSHOT_DIR = $screenshotRoot
  $env:LYNEWRIGHT_DEV_API_TARGET = "http://127.0.0.1:$apiPort"
  $env:LYNEWRIGHT_E2E_OUTPUT_DIR = Join-Path $dataRoot "playwright-results"

  $apiOut = Join-Path $dataRoot "api.stdout.log"
  $apiErr = Join-Path $dataRoot "api.stderr.log"
  $webOut = Join-Path $dataRoot "web.stdout.log"
  $webErr = Join-Path $dataRoot "web.stderr.log"
  $python = (Get-Command python.exe -ErrorAction Stop).Source
  $npm = (Get-Command npm.cmd -ErrorAction Stop).Source
  $apiPort = Get-FreeLoopbackPort
  $webPort = Get-FreeLoopbackPort
  $cataloguePort = Get-FreeLoopbackPort
  $env:LYNEWRIGHT_CATALOGUE_URL = "https://catalogue.example.test/catalogue.json"
  $env:LYNEWRIGHT_CATALOGUE_ALLOW_DEV_KEY = "1"
  $env:LYNEWRIGHT_CATALOGUE_INTERNAL_ENDPOINT_REF = "U1B_CATALOGUE"
  $env:LYNEWRIGHT_INTERNAL_ENDPOINT__U1B_CATALOGUE = "http://127.0.0.1:$cataloguePort"
  $env:LYNEWRIGHT_SCREENSHOT_BASE_URL = "http://127.0.0.1:$webPort"
  $env:LYNEWRIGHT_DEV_API_TARGET = "http://127.0.0.1:$apiPort"

  $catalogueOut = Join-Path $dataRoot "catalogue.stdout.log"
  $catalogueErr = Join-Path $dataRoot "catalogue.stderr.log"
  $catalogueProcess = Start-Process -FilePath $python -ArgumentList @(
    "-m", "http.server", "$cataloguePort", "--bind", "127.0.0.1", "--directory", $catalogueDist
  ) -WorkingDirectory $catalogueRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput $catalogueOut -RedirectStandardError $catalogueErr
  Wait-Http "http://127.0.0.1:$cataloguePort/catalogue.json" 30 "Signed development catalogue"

  $apiProcess = Start-Process -FilePath $python -ArgumentList @(
    "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "$apiPort"
  ) -WorkingDirectory $backendRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput $apiOut -RedirectStandardError $apiErr
  Wait-Http "http://127.0.0.1:$apiPort/health" 90 "Isolated demo API"
  $apiProcess.Refresh()
  if ($apiProcess.HasExited) { throw "The isolated API process exited with code $($apiProcess.ExitCode)." }

  $webProcess = Start-Process -FilePath $npm -ArgumentList @(
    "run", "dev", "--", "--port", "$webPort"
  ) -WorkingDirectory $frontendRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput $webOut -RedirectStandardError $webErr
  Wait-Http "http://127.0.0.1:$webPort/overview" 60 "Vite dashboard"
  $webProcess.Refresh()
  if ($webProcess.HasExited) { throw "The isolated Vite process exited with code $($webProcess.ExitCode)." }

  Push-Location $frontendRoot
  try {
    if ($env:LYNEWRIGHT_E2E_GREP) { & $npm run e2e -- --grep $env:LYNEWRIGHT_E2E_GREP }
    else { & $npm run e2e }
    if ($LASTEXITCODE -ne 0) { throw "Playwright exited with code $LASTEXITCODE" }
  }
  finally { Pop-Location }

  Write-Output "U1 screenshots: $screenshotRoot"
  Write-Output "Temporary data directory: removed after this run"
} catch {
  Write-Error $_
  $resultRoot = Join-Path $dataRoot "playwright-results"
  if (Test-Path -LiteralPath $resultRoot) {
    Get-ChildItem -LiteralPath $resultRoot -Recurse -File -Filter "error-context.md" | ForEach-Object {
      Write-Output ("Playwright failure context: " + $_.FullName)
      Get-Content -LiteralPath $_.FullName -TotalCount 100
    }
  }
  if (Test-Path (Join-Path $dataRoot "api.stdout.log")) { Get-Content (Join-Path $dataRoot "api.stdout.log") -Tail 60 }
  if (Test-Path (Join-Path $dataRoot "api.stderr.log")) { Get-Content (Join-Path $dataRoot "api.stderr.log") -Tail 60 }
  if (Test-Path (Join-Path $dataRoot "web.stdout.log")) { Get-Content (Join-Path $dataRoot "web.stdout.log") -Tail 30 }
  if (Test-Path (Join-Path $dataRoot "web.stderr.log")) { Get-Content (Join-Path $dataRoot "web.stderr.log") -Tail 30 }
  throw
} finally {
  if ($webProcess -and -not $webProcess.HasExited) {
    & taskkill.exe /PID $webProcess.Id /T /F 2>$null | Out-Null
  }
  if ($catalogueProcess -and -not $catalogueProcess.HasExited) { Stop-Process -Id $catalogueProcess.Id -Force -ErrorAction SilentlyContinue }
  if ($apiProcess -and -not $apiProcess.HasExited) { Stop-Process -Id $apiProcess.Id -Force -ErrorAction SilentlyContinue }

  foreach ($name in $environmentNames) {
    $value = $savedEnvironment[$name]
    if ($null -eq $value) { [Environment]::SetEnvironmentVariable($name, $null, "Process") }
    else { [Environment]::SetEnvironmentVariable($name, $value, "Process") }
  }

  if (Test-Path -LiteralPath $dataRoot) {
    $resolvedData = (Resolve-Path -LiteralPath $dataRoot).Path.TrimEnd('\')
    $requiredPrefix = $tempBase + '\'
    if (-not $resolvedData.StartsWith($requiredPrefix, [StringComparison]::OrdinalIgnoreCase) -or (Split-Path $resolvedData -Leaf) -ne "lynewright-u1-$runId") {
      throw "Refusing to remove unexpected temporary path: $resolvedData"
    }
    Remove-Item -LiteralPath $resolvedData -Recurse -Force
  }
}
