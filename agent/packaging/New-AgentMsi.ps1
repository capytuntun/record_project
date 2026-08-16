<#
.SYNOPSIS
    在 Windows 上建置一份「已帶設定」的 EndpointAgent MSI。

.DESCRIPTION
    管理伺服器跑在 Linux 時，主控台的「安裝包」功能無法使用 —— WiX 只支援
    Windows（v5 在其他平台會直接印 "The WiX Toolset only supports Windows"
    並產出失敗）。這支腳本在 Windows 上做和伺服器 app/services/packaging.py
    完全相同的事：把伺服器網址、註冊憑證、管理密碼雜湊包進 MSI。

    流程：主控台建立註冊憑證 → 複製憑證字串 → 在 Windows 上跑這支腳本 →
    得到 MSI → 用 GPO／SCCM 派送。

.PARAMETER ServerUrl
    端點要回連的管理伺服器網址，必須是 https://（例：https://eem.example.com）。

.PARAMETER EnrollmentToken
    主控台「安裝包 → 註冊憑證」產生的憑證字串。給整個車隊共用的話，建議在
    主控台把它設成「不限次數」（永不過期與否見 CLAUDE.md §18.1 的風險說明）。

.PARAMETER AdminPassword
    端點本機的管理密碼。沒設就無法在端點上用 set-server／reset-enrollment
    改設定（agent 會拒絕），也無法用密碼保護解除安裝。只存雜湊，明文不進 MSI。

.EXAMPLE
    .\New-AgentMsi.ps1 -ServerUrl https://eem.example.com -EnrollmentToken 'xxxx' `
                       -AdminPassword 'ITonly-2026' -Label 台北辦公室

.EXAMPLE
    # 連同簽章（憑證密碼放環境變數，不進命令列）
    $env:EEM_SIGNING_PASSWORD = 'pfx 密碼'
    .\New-AgentMsi.ps1 -ServerUrl https://eem.example.com -EnrollmentToken 'xxxx' `
                       -CertPath C:\certs\eem.pfx
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ServerUrl,
    [Parameter(Mandatory = $true)][string]$EnrollmentToken,
    [string]$AdminPassword,
    [string]$Label = "endpoint-agent",
    [string]$OrganizationId,
    [string]$AgentVersion = "0.1.0",
    [int]$HeartbeatSeconds = 60,
    [string]$OutputPath,
    [string]$CertPath,
    [string]$TimestampUrl
)

$ErrorActionPreference = "Stop"

# 必須和 app/services/packaging.py 的 hash_admin_password 一致，
# 否則 agent 端的 AdminPassword.Verify 驗不過。
$PBKDF2_ITERATIONS = 210000
$PBKDF2_SALT_BYTES = 16
$PBKDF2_HASH_BYTES = 32

function New-AdminPasswordHash {
    param([string]$Password)
    $salt = New-Object byte[] $PBKDF2_SALT_BYTES
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($salt)
    $kdf = New-Object System.Security.Cryptography.Rfc2898DeriveBytes(
        $Password, $salt, $PBKDF2_ITERATIONS,
        [System.Security.Cryptography.HashAlgorithmName]::SHA256)
    try   { $hash = $kdf.GetBytes($PBKDF2_HASH_BYTES) }
    finally { $kdf.Dispose() }
    "pbkdf2-sha256`$$PBKDF2_ITERATIONS`$$([Convert]::ToBase64String($salt))`$$([Convert]::ToBase64String($hash))"
}

if ($ServerUrl -notmatch '^https://') {
    throw "ServerUrl 必須是 https://（http 會讓裝置憑證以明文上線，見 CLAUDE.md §24）。"
}

$agentRoot = Split-Path -Parent $PSScriptRoot
$binary    = Join-Path $agentRoot "publish\EndpointAgent.exe"
$wxs       = Join-Path $PSScriptRoot "EndpointAgent.wxs"
$caDll     = Join-Path $agentRoot "src\EndpointAgent.CustomActions\bin\Release\net472\EndpointAgent.CustomActions.CA.dll"

if (-not (Get-Command wix -ErrorAction SilentlyContinue)) {
    throw @"
找不到 WiX。請先安裝（需要 .NET SDK）：
    dotnet tool install --global wix --version 5.*
    wix extension add -g WixToolset.Util.wixext/5.0.2
    wix extension add -g WixToolset.Firewall.wixext/5.0.2
若剛裝好仍找不到，開一個新的 PowerShell 視窗讓 PATH 生效。
"@
}
if (-not (Test-Path $binary)) { throw "找不到 Agent 執行檔：$binary（先在 agent\ 執行 .\build.ps1）" }
if (-not (Test-Path $caDll))  { throw "找不到解除安裝自訂動作 DLL：$caDll（先建置 EndpointAgent.CustomActions）" }
if (-not (Test-Path $wxs))    { throw "找不到 WiX 封裝定義：$wxs" }

if (-not $OutputPath) {
    $safe = ($Label -replace '[^A-Za-z0-9._\-]+', '-').Trim('-.', [System.StringSplitOptions]::None)
    if (-not $safe) { $safe = "endpoint-agent" }
    $OutputPath = Join-Path (Get-Location) "$safe.msi"
}

# agent 的起始設定。註冊憑證在這裡是因為 agent 只在第一次啟動時需要它，
# 註冊成功後 agent 會自己從本機設定裡清掉。
$config = [ordered]@{
    serverUrl                 = $ServerUrl
    enrollmentToken           = $EnrollmentToken
    logLevel                  = "Information"
    heartbeatIntervalSeconds  = $HeartbeatSeconds
}
if ($OrganizationId) { $config["organizationId"]   = $OrganizationId }
if ($AdminPassword)  { $config["adminPasswordHash"] = New-AdminPasswordHash -Password $AdminPassword }
else { Write-Warning "沒有指定 -AdminPassword：端點上將無法用 set-server／reset-enrollment 變更設定。" }

$work = Join-Path ([System.IO.Path]::GetTempPath()) ("eem-pkg-" + [System.Guid]::NewGuid().ToString("N").Substring(0, 8))
New-Item -ItemType Directory -Path $work | Out-Null
try {
    $configFile = Join-Path $work "agent.config.json"
    # 不要 BOM：agent 端用 System.Text.Json 讀這個檔。
    [System.IO.File]::WriteAllText($configFile, ($config | ConvertTo-Json), (New-Object System.Text.UTF8Encoding($false)))

    $staged = Join-Path $work "package.msi"
    $wixArgs = @(
        "build", $wxs,
        # 64 位元套件：裝到 Program Files（非 x86），登錄檔寫進原生區。
        "-arch", "x64",
        "-ext", "WixToolset.Util.wixext",
        "-ext", "WixToolset.Firewall.wixext",
        "-d", "AgentBinary=$binary",
        "-d", "CustomActionDll=$caDll",
        "-d", "ConfigFile=$configFile",
        "-d", "ServerUrl=$ServerUrl",
        "-d", "OrgId=$OrganizationId",
        "-d", "PackageLabel=$Label",
        "-d", "AgentVersion=$AgentVersion",
        "-o", $staged
    )

    Write-Host "建置中：$Label ($ServerUrl)" -ForegroundColor Cyan
    & wix @wixArgs
    if ($LASTEXITCODE -ne 0) { throw "wix build 失敗（離開碼 $LASTEXITCODE）。" }
    if (-not (Test-Path $staged)) { throw "wix build 沒有產生 MSI。" }

    if ($CertPath) {
        $signer = Join-Path $agentRoot "signing\Sign-File.ps1"
        if (-not (Test-Path $signer)) { throw "找不到簽章腳本：$signer" }
        if (-not $env:EEM_SIGNING_PASSWORD) { throw "請先設定環境變數 EEM_SIGNING_PASSWORD（憑證密碼不放命令列）。" }
        $signArgs = @{ File = $staged; CertPath = $CertPath }
        if ($TimestampUrl) { $signArgs["TimestampUrl"] = $TimestampUrl }
        & $signer @signArgs
        if ($LASTEXITCODE -ne 0) { throw "簽章失敗。" }
    }

    Move-Item -Path $staged -Destination $OutputPath -Force
}
finally {
    Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue
}

$item = Get-Item $OutputPath
$sha  = (Get-FileHash $OutputPath -Algorithm SHA256).Hash.ToLower()
Write-Host ""
Write-Host "完成：$($item.FullName)" -ForegroundColor Green
Write-Host ("  大小    {0:N0} bytes" -f $item.Length)
Write-Host "  SHA256  $sha"
Write-Host "  簽章    $(if ($CertPath) { '已簽章' } else { '未簽章（SmartScreen／AppLocker 可能會擋）' })"
Write-Host ""
Write-Host "派送後在端點上安裝：msiexec /i `"$($item.Name)`" /qn"
