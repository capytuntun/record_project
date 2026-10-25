<#
.SYNOPSIS
    螢幕測錄系統（管理伺服器）—— 完全移除。

.DESCRIPTION
    以系統管理員身分執行：

        powershell -ExecutionPolicy Bypass -File server\uninstall.ps1

    停止並移除排程工作、結束相關行程（python、錄影中的 ffmpeg、建置中的 wix）、
    移除防火牆規則，再刪除整個安裝目錄 —— 資料庫、錄影、截圖、加密金鑰、
    TLS 憑證、.env、FFmpeg、WiX 全部刪掉，無法復原。刪除前會要求輸入 DELETE 確認。

    不會動的：Python 與 .NET SDK（共用元件）、.env 指到安裝目錄外的資料、
    已拋轉到 FTP／SMB 的錄影、各端點上的 Agent（請在端點用 msiexec /x 移除）。

    實際工作由同目錄的 install.ps1 -Uninstall 完成，這支只是好記的入口。

.PARAMETER InstallDir
    安裝位置，預設 C:\EEM。

.PARAMETER KeepData
    只移除服務與防火牆規則，保留安裝目錄（之後重跑 install.ps1 可原樣復原）。

.PARAMETER Force
    略過 DELETE 確認（無人值守用）。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File server\uninstall.ps1

.EXAMPLE
    # 只停用服務，資料留著
    .\uninstall.ps1 -KeepData
#>
[CmdletBinding()]
param(
    [string]$InstallDir = "C:\EEM",
    [switch]$KeepData,
    [switch]$Force
)

$installer = Join-Path $PSScriptRoot "install.ps1"
if (-not (Test-Path -LiteralPath $installer)) {
    Write-Host "找不到 $installer（uninstall.ps1 要和 install.ps1 放在同一個資料夾）。" -ForegroundColor Red
    exit 1
}

$params = @{ Uninstall = $true; InstallDir = $InstallDir }
if (-not $KeepData) { $params.Purge = $true }
if ($Force) { $params.Force = $true }

& $installer @params
exit $LASTEXITCODE
