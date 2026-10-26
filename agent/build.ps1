<#
.SYNOPSIS
    Publishes the endpoint agent binary that the package generator wraps.

.DESCRIPTION
    Run this once after changing agent source. The management server does NOT
    rebuild the agent per download -- it only rebuilds the MSI wrapper around
    the binary this script produces, which is why generating a package takes
    seconds rather than minutes.

    Output: agent/publish/EndpointAgent.exe (self-contained, single file).

.PARAMETER SkipWixCheck
    Skip the WiX presence check. server/install.ps1 passes this because it
    builds the agent first and provisions its own WiX (outside PATH) afterwards.

.EXAMPLE
    ./build.ps1
#>
[CmdletBinding()]
param(
    [string]$Configuration = "Release",
    [switch]$SkipWixCheck
)

$ErrorActionPreference = "Stop"

# When the output is captured (a log file, server/install.ps1), write UTF-8.
# dotnet writes UTF-8 to a pipe; decoding it with the default code page (cp950
# on Traditional Chinese Windows) turns its localized messages into mojibake.
if ([Console]::IsOutputRedirected) {
    try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false } catch { }
}

$root = $PSScriptRoot
$project = Join-Path $root "src/EndpointAgent/EndpointAgent.csproj"
$caProject = Join-Path $root "src/EndpointAgent.CustomActions/EndpointAgent.CustomActions.csproj"
$publish = Join-Path $root "publish"

Write-Host "Publishing agent..." -ForegroundColor Cyan
dotnet publish $project -c $Configuration -o $publish
if ($LASTEXITCODE -ne 0) { throw "dotnet publish failed with exit code $LASTEXITCODE" }

# The uninstall-password custom action. The package generator's WiX build
# references its .CA.dll, so it must be built before any package is generated.
Write-Host "Building uninstall custom action..." -ForegroundColor Cyan
dotnet build $caProject -c $Configuration
if ($LASTEXITCODE -ne 0) { throw "custom action build failed with exit code $LASTEXITCODE" }

$exe = Join-Path $publish "EndpointAgent.exe"
if (-not (Test-Path $exe)) { throw "Expected $exe was not produced." }

$size = [math]::Round((Get-Item $exe).Length / 1MB, 1)
Write-Host "OK  $exe  ($size MB)" -ForegroundColor Green

# Sign the agent binary once here, if a certificate is configured. The MSI that
# wraps it is signed separately, per package, by the server. Signing the exe too
# means AppLocker/AV see a signed executable even before the MSI is trusted.
if ($env:EEM_SIGNING_PFX -and $env:EEM_SIGNING_PASSWORD) {
    $signer = Join-Path $root "signing/Sign-File.ps1"
    $ts = if ($env:EEM_SIGNING_TIMESTAMP_URL) { $env:EEM_SIGNING_TIMESTAMP_URL } else { "" }
    & $signer -File $exe -CertPath $env:EEM_SIGNING_PFX -TimestampUrl $ts
} else {
    Write-Warning "EEM_SIGNING_PFX not set; agent binary is unsigned. See signing/README.md."
}

# WiX is what turns that binary into an installable package. The server uses
# EEM_WIX_COMMAND when set, so honour it here too before falling back to PATH.
$wix = if ($env:EEM_WIX_COMMAND) { Get-Command $env:EEM_WIX_COMMAND -ErrorAction SilentlyContinue }
       else { Get-Command wix -ErrorAction SilentlyContinue }
if ($SkipWixCheck) {
    # The caller handles WiX itself.
} elseif (-not $wix) {
    Write-Warning "WiX not found. The server cannot build packages until you run:"
    Write-Warning "  dotnet tool install --global wix --version 5.*"
    Write-Warning "  wix extension add -g WixToolset.Util.wixext/5.0.2"
    Write-Warning "  wix extension add -g WixToolset.Firewall.wixext/5.0.2"
    Write-Warning "  wix extension add -g WixToolset.UI.wixext/5.0.2"
    Write-Warning "Use WiX 5, not 6/7 -- those require accepting a paid maintenance-fee EULA."
} else {
    Write-Host "OK  wix $((& $wix.Source --version) -join '')" -ForegroundColor Green
}

Write-Host ""
Write-Host "The server picks this up automatically at agent/publish/EndpointAgent.exe."
Write-Host "Override with EEM_AGENT_BINARY if you publish elsewhere."
