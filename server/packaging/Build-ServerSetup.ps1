<#
.SYNOPSIS
    把管理伺服器打包成一個「下一步、下一步」的圖形安裝檔（EEMServerSetup-*.exe）。

.DESCRIPTION
    做兩件事：
      1) 把要隨安裝檔帶走的東西備到 build\payload\（server 原始碼、Agent 素材與
         原始碼、docs、README）。重的 .venv、instance、__pycache__ 一律排除。
      2) 用 Inno Setup 的 ISCC 把 EEMServer.iss 連同 payload 編成單一安裝檔，
         輸出到 dist\。

    找不到 Inno Setup（ISCC.exe）時，會用 winget 自動安裝（可能跳出 UAC）。

    產出的安裝檔只是外殼：實際安裝邏輯仍在 server\install.ps1，精靈執行時呼叫它。

.PARAMETER AppVer
    版本字串（顯示在「新增/移除程式」與檔名）。預設為今天日期，如 2026.10.26。

.PARAMETER Lean
    不夾帶內附的 FFmpeg（server\tools\ffmpeg\ffmpeg.zip，約 37 MB）。安裝檔會小很多，
    但目標電腦若要錄影就得在安裝時連網下載 FFmpeg。

.PARAMETER SkipInnoInstall
    找不到 ISCC 時不要自動用 winget 安裝，直接失敗。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File server\packaging\Build-ServerSetup.ps1

.EXAMPLE
    .\Build-ServerSetup.ps1 -AppVer 1.2.0 -Lean
#>
[CmdletBinding()]
param(
    [string]$AppVer,
    [switch]$Lean,
    [switch]$SkipInnoInstall
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

if ([Console]::IsOutputRedirected) {
    try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false } catch { }
}

function Step($m) { Write-Host "`n▶ $m" -ForegroundColor White }
function Info($m) { Write-Host "   $m" }
function Ok($m)   { Write-Host "   $([char]0x2713) $m" -ForegroundColor Green }
function Warn($m) { Write-Host "   ! $m" -ForegroundColor Yellow }
function Die($m)  { Write-Host "`n$([char]0x2717) $m" -ForegroundColor Red; exit 1 }

$PkgDir   = $PSScriptRoot
$RepoRoot = (Resolve-Path (Join-Path $PkgDir "..\..")).Path
$Stage    = Join-Path $PkgDir "build\payload"
$DistDir  = Join-Path $PkgDir "dist"
$Iss      = Join-Path $PkgDir "EEMServer.iss"

if (-not $AppVer) { $AppVer = Get-Date -Format "yyyy.MM.dd" }
if (-not (Test-Path $Iss)) { Die "找不到 $Iss。" }
if (-not (Test-Path (Join-Path $RepoRoot "server\install.ps1"))) {
    Die "找不到 server\install.ps1（RepoRoot 解析成 $RepoRoot，看起來不對）。"
}

# robocopy 的結束碼 < 8 都算成功（0=沒變動、1=有複製、2/3=有多餘等）。
function Invoke-Robocopy {
    param([string]$Src, [string]$Dst, [string[]]$Extra)
    $null = robocopy $Src $Dst /MIR /NFL /NDL /NJH /NJS /NP @Extra
    if ($LASTEXITCODE -ge 8) { Die "複製失敗（robocopy 代碼 $LASTEXITCODE）：$Src → $Dst" }
}

# ---------------------------------------------------------------- 備好 payload

Step "準備打包內容 → $Stage"
if (Test-Path $Stage) { Remove-Item $Stage -Recurse -Force }
New-Item -ItemType Directory -Force -Path $Stage | Out-Null

# server 原始碼：排除虛擬環境、執行期資料、快取與打包用資料夾本身。
$serverExclude = @("/XD", ".venv", "instance", "__pycache__", ".pytest_cache", "packaging", "/XF", ".env")
if ($Lean) {
    # 連內附的 FFmpeg 一起排除（但保留授權/來源說明以外的目錄結構）。
    $serverExclude += @("ffmpeg.zip")
}
Invoke-Robocopy (Join-Path $RepoRoot "server") (Join-Path $Stage "server") $serverExclude
$ffZip = Join-Path $Stage "server\tools\ffmpeg\ffmpeg.zip"
if ($Lean) { Info "精簡模式：不夾帶 FFmpeg（目標電腦要錄影需連網下載）" }
elseif (Test-Path $ffZip) { Ok "已夾帶內附 FFmpeg（$([math]::Round((Get-Item $ffZip).Length/1MB,1)) MB）" }
else { Warn "找不到內附的 ffmpeg.zip，安裝時將改為連網下載" }

# Agent：原始碼（讓主控台可重建）＋ 既有的 publish 執行檔（讓主控台免編譯就能產生安裝包）
Invoke-Robocopy (Join-Path $RepoRoot "agent\src") (Join-Path $Stage "agent\src") @("/XD", "bin", "obj")
$agentExe = Join-Path $RepoRoot "agent\publish\EndpointAgent.exe"
if (Test-Path $agentExe) {
    Invoke-Robocopy (Join-Path $RepoRoot "agent\publish") (Join-Path $Stage "agent\publish") @()
    Ok "已夾帶 Agent 執行檔（$([math]::Round((Get-Item $agentExe).Length/1MB,1)) MB）"
} else {
    Warn "找不到 agent\publish\EndpointAgent.exe —— 先在 agent\ 執行 build.ps1。"
    Warn "安裝後主控台首次產生安裝包時，install.ps1 會嘗試用 .NET SDK 從原始碼重建。"
}

# 解除安裝密碼保護用的自訂動作 DLL：上面複製 agent\src 時用 /XD bin 排除了 bin，
# 但主控台建安裝包一定要這個 .CA.dll（否則報「找不到解除安裝自訂動作 DLL」）。
# 所以像 EndpointAgent.exe 一樣，把建好的產物單獨夾帶進來。
$caOutDir = Join-Path $RepoRoot "agent\src\EndpointAgent.CustomActions\bin\Release\net472"
$caDll    = Join-Path $caOutDir "EndpointAgent.CustomActions.CA.dll"
if (Test-Path $caDll) {
    $caStage = Join-Path $Stage "agent\src\EndpointAgent.CustomActions\bin\Release\net472"
    Invoke-Robocopy $caOutDir $caStage @()
    Ok "已夾帶解除安裝自訂動作 DLL（$([math]::Round((Get-Item $caDll).Length/1KB,0)) KB）"
} else {
    Warn "找不到 CA.dll（agent\src\EndpointAgent.CustomActions\bin\Release\net472）。"
    Warn "主控台將無法建安裝包；請先在 agent\ 執行 build.ps1 後重新打包。"
}

foreach ($rel in @("agent\packaging", "agent\signing", "docs")) {
    $from = Join-Path $RepoRoot $rel
    if (Test-Path $from) { Invoke-Robocopy $from (Join-Path $Stage $rel) @() }
}
Copy-Item (Join-Path $RepoRoot "README.md") (Join-Path $Stage "README.md") -Force -ErrorAction SilentlyContinue

$payloadMB = [math]::Round((Get-ChildItem $Stage -Recurse -File | Measure-Object Length -Sum).Sum / 1MB, 1)
Ok "打包內容已就緒（$payloadMB MB）"

# ---------------------------------------------------------------- 找 / 裝 Inno Setup

function Resolve-Iscc {
    # winget 可能把 Inno Setup 裝到機器層級（Program Files）或使用者層級
    # （%LOCALAPPDATA%\Programs），註冊資訊也就分別落在 HKLM 或 HKCU，兩邊都要找。
    $cands = @(
        (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
        (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe")
    )
    foreach ($key in @(
        "HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1",
        "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1",
        "HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1")) {
        try {
            $loc = (Get-ItemProperty -Path $key -ErrorAction Stop).InstallLocation
            if ($loc) { $cands += (Join-Path $loc "ISCC.exe") }
        } catch { }
    }
    $cmd = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($cmd) { $cands += $cmd.Source }
    foreach ($c in ($cands | Select-Object -Unique)) { if ($c -and (Test-Path $c)) { return $c } }
    return $null
}

Step "尋找 Inno Setup 編譯器（ISCC）"
$iscc = Resolve-Iscc
if (-not $iscc) {
    if ($SkipInnoInstall) {
        Die "找不到 Inno Setup。請先安裝（https://jrsoftware.org/isdl.php）或移除 -SkipInnoInstall。"
    }
    Info "找不到 ISCC，嘗試用 winget 安裝 Inno Setup（可能跳出 UAC 要求授權）…"
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Die "這台沒有 winget，無法自動安裝。請手動安裝 Inno Setup 後重跑。"
    }
    & winget install --id JRSoftware.InnoSetup --source winget --silent `
        --accept-package-agreements --accept-source-agreements | Out-Host
    $env:PATH = [Environment]::GetEnvironmentVariable("PATH", "Machine") + ";" + $env:PATH
    $iscc = Resolve-Iscc
    if (-not $iscc) { Die "Inno Setup 安裝後仍找不到 ISCC.exe。請手動確認安裝是否成功。" }
}
Ok "ISCC：$iscc"

# ---------------------------------------------------------------- 編譯

Step "編譯安裝檔（版本 $AppVer）"
New-Item -ItemType Directory -Force -Path $DistDir | Out-Null
# /O 與 .iss 內的 OutputDir 都是相對「目前工作目錄」解析，不是相對 .iss。這裡傳絕對
# 路徑，產出才會固定落在 packaging\dist（已被 .gitignore 忽略），不會跑到 repo 根目錄。
& $iscc "/DAppVer=$AppVer" "/DPayloadDir=$Stage" "/O$DistDir" $Iss | Out-Host
if ($LASTEXITCODE -ne 0) { Die "ISCC 編譯失敗（代碼 $LASTEXITCODE）。" }

$setup = Join-Path $DistDir "EEMServerSetup-$AppVer.exe"
if (-not (Test-Path $setup)) { Die "編譯完成但找不到預期的輸出 $setup。" }
$setupMB = [math]::Round((Get-Item $setup).Length / 1MB, 1)

Write-Host ""
Write-Host "══════════════════════════════════════════════════════════" -ForegroundColor Green
Write-Host " 安裝檔已產生" -ForegroundColor White
Write-Host "══════════════════════════════════════════════════════════" -ForegroundColor Green
Write-Host ""
Write-Host "  檔案   $setup（$setupMB MB）"
Write-Host "  用法   在目標電腦上按兩下，依「下一步」完成安裝（需系統管理員權限）。"
Write-Host "  靜默   EEMServerSetup-$AppVer.exe /VERYSILENT /SUPPRESSMSGBOXES"
Write-Host ""
