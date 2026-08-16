<#
.SYNOPSIS
    螢幕測錄系統（管理伺服器）—— Windows 一鍵安裝。

.DESCRIPTION
    以系統管理員身分執行一行就好：

        powershell -ExecutionPolicy Bypass -File server\install.ps1

    做完這些事：Python 虛擬環境 → 隨機密鑰 .env → 自簽 TLS 憑證 → 建資料庫 →
    建第一個最高管理員 → 開機自動啟動的排程工作 → 防火牆規則 → .NET SDK + WiX
    （讓主控台可以直接產生 MSI 安裝包）。

    缺 Python 3.11+ 或 .NET SDK 時會用 winget 自動補齊，不需要事先手動安裝。

    和 Linux 版（install.sh）的差別：Windows 單機不另外架反向代理，**由程式自己
    終結 TLS**（0.0.0.0:443），所以裝完就能用 https:// 連。

    重跑 = 就地升級：程式碼換新，.env 與 instance\（資料庫、金鑰、錄影）保留。

.PARAMETER InstallDir
    安裝位置，預設 C:\EEM。

.PARAMETER Port
    對外的 HTTPS 埠，預設 443。

.PARAMETER CertPath
    自己的憑證（.pfx）。不給就產生自簽憑證。

.PARAMETER Uninstall
    移除排程工作與防火牆規則（資料保留）。加 -Purge 連同資料一併刪除。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File server\install.ps1

.EXAMPLE
    # 用公司憑證、換個埠
    .\install.ps1 -CertPath C:\certs\eem.pfx -CertPassword 'pfx密碼' -Port 8443
#>
[CmdletBinding()]
param(
    [string]$InstallDir = "C:\EEM",
    [string]$AdminUser = "admin",
    [int]$Port = 443,
    [string]$CertPath,
    [string]$CertPassword,
    [switch]$NoPackaging,
    [switch]$NoFirewall,
    [switch]$Uninstall,
    [switch]$Purge
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$TaskName     = "EEMManagementServer"
$FirewallRule = "螢幕測錄系統"
$AppDir       = Join-Path $InstallDir "server"
$HomeDir      = Join-Path $InstallDir "home"      # 服務行程的 USERPROFILE（WiX 擴充放這）
$VenvPy       = Join-Path $AppDir ".venv\Scripts\python.exe"

function Step($m) { Write-Host "`n▶ $m" -ForegroundColor White }
function Info($m) { Write-Host "   $m" }
function Ok($m)   { Write-Host "   $([char]0x2713) $m" -ForegroundColor Green }
function Warn($m) { Write-Host "   ! $m" -ForegroundColor Yellow }
function Die($m)  {
    # 也寫到 stderr：輸出被導向檔案時，Write-Host 的資訊串流可能來不及沖出，
    # 失敗原因就會消失在記錄檔裡。
    Write-Host "`n$([char]0x2717) $m" -ForegroundColor Red
    [Console]::Error.WriteLine("ERROR: $m")
    exit 1
}

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-Admin)) {
    Die "請用系統管理員身分執行（右鍵 PowerShell → 以系統管理員身分執行）。"
}

# ---------------------------------------------------------------- 解除安裝

function Remove-Installation {
    Step "移除螢幕測錄系統服務"
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($task) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Ok "已停止並移除排程工作"
    } else {
        Info "沒有找到排程工作 $TaskName"
    }
    # 服務行程可能還在跑（排程工作只是啟動它）
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like "*$AppDir*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

    if (Get-NetFirewallRule -DisplayName $FirewallRule -ErrorAction SilentlyContinue) {
        Remove-NetFirewallRule -DisplayName $FirewallRule
        Ok "已移除防火牆規則"
    }

    if ($Purge) {
        Warn "-Purge：將刪除 $InstallDir（含資料庫、錄影、加密金鑰）"
        Remove-Item $InstallDir -Recurse -Force -ErrorAction SilentlyContinue
        Ok "已刪除 $InstallDir"
    } else {
        Info "資料保留在 $InstallDir（要一併刪除請加 -Purge）"
    }
    Write-Host "`n解除安裝完成。" -ForegroundColor Green
}

if ($Uninstall) { Remove-Installation; exit 0 }
if ($Purge) { Die "-Purge 只能搭配 -Uninstall 使用（它會刪除資料庫與錄影）。" }

# ---------------------------------------------------------------- 來源與 Python

Write-Host "螢幕測錄系統 —— Windows 安裝" -ForegroundColor White
Write-Host "安裝位置 $InstallDir ・ 連接埠 $Port" -ForegroundColor DarkGray

$srcServer = $PSScriptRoot
if (-not (Test-Path (Join-Path $srcServer "wsgi.py"))) {
    Die "找不到 wsgi.py。請直接執行原始碼裡的 server\install.ps1。"
}
$srcRoot = Split-Path -Parent $srcServer

function Invoke-Winget {
    # 回傳 winget 的完整輸出（含 stderr）。這段輸出是「為什麼裝不起來」的唯一線索：
    # 之前用 | Out-Null 吞掉，失敗時畫面上只剩「找不到 Python」，完全無從查起。
    param([string[]]$WingetArgs)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"   # 同 Invoke-Flask：原生程式的 stderr 不是致命錯誤
    try {
        $out = & winget @WingetArgs 2>&1 | ForEach-Object {
            if ($_ -is [System.Management.Automation.ErrorRecord]) { $_.ToString() } else { "$_" }
        }
    } catch {
        $out = @("winget 執行失敗：$($_.Exception.Message)")
    } finally {
        $ErrorActionPreference = $prev
    }
    return ($out -join "`n")
}

function Show-WingetOutput {
    param([string]$Output)
    Warn "winget 的輸出（最後 12 行非空白）："
    $lines = @($Output -split "`r?`n" | Where-Object { $_.Trim() } | Select-Object -Last 12)
    if ($lines) { $lines | ForEach-Object { Info "  $_" } } else { Info "  （沒有輸出）" }
}

function Test-StoreAliasStub {
    # Microsoft Store 的「應用程式執行別名」：0 位元組的重解析點（AppExecLink）。
    # 執行它不會跑 Python，只會印
    #   Python was not found; run without arguments to install from the Microsoft Store...
    # 先認出來就能在輸出裡講清楚為什麼跳過，而不是讓使用者看到莫名其妙的
    # 「找不到 Python」——或更糟，帶著它往下跑到建立虛擬環境才爆。
    param([string]$Path)
    try {
        $f = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
        return ($f.Length -eq 0) -and ($f.Attributes -band [IO.FileAttributes]::ReparsePoint)
    } catch { return $false }
}

function Resolve-Python {
    # 一律以「實際跑一次 -c」的結果判斷，不能只看 Get-Command 找不找得到：
    # 轉接殼存在、Get-Command 也找得到，但它不是 Python。
    # 探測失敗（$ver 為空）就換下一個候選。
    $candidates = @()
    $stubs = @()
    foreach ($cand in @("py -3.13", "py -3.12", "py -3.11", "python", "python3")) {
        $parts = $cand.Split(" ")
        $cmd = Get-Command $parts[0] -ErrorAction SilentlyContinue
        if (-not $cmd) { continue }
        if (Test-StoreAliasStub $cmd.Source) { $stubs += $cmd.Source; continue }
        # $args 是 PowerShell 的自動變數，別覆寫它。
        $pyArgs = @()
        if ($parts.Count -gt 1) { $pyArgs += $parts[1] }
        $candidates += , @($cmd.Source, $pyArgs)
    }
    if ($stubs) {
        Info "略過 Microsoft Store 轉接殼（不是真的 Python）：$(($stubs | Select-Object -Unique) -join '、')"
    }
    # winget 剛裝完時 PATH 不一定在這個行程生效，所以也直接看預設安裝位置。
    foreach ($v in @("313", "312", "311")) {
        foreach ($base in @($env:ProgramFiles, (Join-Path $env:LOCALAPPDATA "Programs\Python"))) {
            if (-not $base) { continue }
            $p = Join-Path $base "Python$v\python.exe"
            if (Test-Path $p) { $candidates += , @($p, @()) }
        }
    }
    foreach ($c in $candidates) {
        $exe = $c[0]
        $pyArgs = $c[1]
        try {
            $ver = & $exe @pyArgs -c "import sys;print('%d.%d' % sys.version_info[:2])" 2>$null
            if ($ver -and [version]$ver -ge [version]"3.11") {
                return @{ Exe = $exe; Args = $pyArgs; Version = "$ver" }
            }
        } catch { continue }
    }
    return $null
}

function Resolve-DotnetSdk {
    # 有 dotnet.exe 不代表有 SDK —— 只裝執行階段（runtime）時 dotnet.exe 一樣在，
    # 但 `dotnet tool install` 需要 SDK。所以實際問 --list-sdks，別只看命令在不在。
    $candidates = @()
    $cmd = Get-Command dotnet -ErrorAction SilentlyContinue
    if ($cmd) { $candidates += $cmd.Source }
    # winget 剛裝完時 PATH 不一定在這個行程生效，所以也直接看預設安裝位置。
    $candidates += (Join-Path $env:ProgramFiles "dotnet\dotnet.exe")
    foreach ($exe in ($candidates | Select-Object -Unique)) {
        if (-not (Test-Path $exe)) { continue }
        $prev = $ErrorActionPreference
        $ErrorActionPreference = "Continue"   # 同 Invoke-Flask：原生程式的 stderr 不是致命錯誤
        try { $sdks = & $exe --list-sdks 2>$null } catch { $sdks = $null } finally { $ErrorActionPreference = $prev }
        if ($sdks | Where-Object { $_ -match '^\d+\.\d+\.\d+' }) { return $exe }
    }
    return $null
}

function Ensure-DotnetSdk {
    # 需要 .NET SDK 的有兩處：建 Agent 執行檔，以及安裝 WiX。兩邊共用同一套
    # 「偵測 → 缺就用 winget 裝 → 重新整理 PATH → 再偵測」流程。
    $dotnet = Resolve-DotnetSdk
    if ($dotnet) { return $dotnet }

    Info "找不到 .NET SDK，嘗試用 winget 安裝…"
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        # --source winget：理由同 Python 那段（msstore 在做 TLS 檢查的網路上會失敗）。
        $wgOut = Invoke-Winget @("install", "--id", "Microsoft.DotNet.SDK.9", "--source", "winget",
                                 "--scope", "machine", "--silent",
                                 "--accept-package-agreements", "--accept-source-agreements")
        # winget 不會更新目前這個行程的 PATH，補上機器層級的 PATH 再找一次。
        $env:PATH = [Environment]::GetEnvironmentVariable("PATH", "Machine") + ";" + $env:PATH
        $dotnet = Resolve-DotnetSdk
        if ($dotnet) { Ok ".NET SDK 已安裝（$dotnet）" } else { Show-WingetOutput $wgOut }
    } else {
        Info "這台沒有 winget，無法自動安裝 .NET SDK。"
    }
    return $dotnet
}

Step "準備 Python"
$python = Resolve-Python
if (-not $python) {
    Info "找不到 Python 3.11 以上，嘗試用 winget 安裝…"
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        # --source winget：只用社群套件庫，不要碰 msstore。企業網路做 TLS 檢查時
        # msstore 會因為憑證釘選失敗（0x8a15005e）而整個查詢中止，winget 接著要求
        # 「請用 --source 指定」，於是連本來找得到的套件都裝不下去。
        $wgArgs = @("install", "--id", "Python.Python.3.12", "--source", "winget", "--silent",
                    "--accept-package-agreements", "--accept-source-agreements")
        $wgOut = Invoke-Winget ($wgArgs + @("--scope", "machine"))
        $env:PATH = [Environment]::GetEnvironmentVariable("PATH", "Machine") + ";" + $env:PATH
        # 重跑同一套偵測。裝完後 `python` 很可能仍解析到 Store 的轉接殼，
        # 直接信任 Get-Command 會拿到一個「執行時才說自己不存在」的假 python，
        # 然後在下一步建立虛擬環境時才爆掉。
        $python = Resolve-Python
        if (-not $python) {
            # 有些 winget／套件組合不接受 machine scope，退回使用者範圍再試一次。
            # 裝到 %LOCALAPPDATA%\Programs\Python 也能用：Resolve-Python 找得到，
            # 而服務跑的是 .venv 裡的直譯器（絕對路徑），不靠 PATH。
            Info "機器範圍安裝沒成功，改用使用者範圍再試一次…"
            $wgOut = Invoke-Winget $wgArgs
            $env:PATH = [Environment]::GetEnvironmentVariable("PATH", "Machine") + ";" + $env:PATH
            $python = Resolve-Python
        }
        if ($python) { Ok "Python 已安裝（$($python.Exe)）" } else { Show-WingetOutput $wgOut }
    } else {
        Info "這台沒有 winget，無法自動安裝 Python。"
    }
}
if (-not $python) {
    Die ("需要 Python 3.11 以上。請到 https://www.python.org/downloads/ 安裝後重跑" +
         "（安裝時勾選 Add to PATH）。若 python 指向 Microsoft Store 的轉接殼，請到" +
         "「設定 → 應用程式 → 進階應用程式設定 → 應用程式執行別名」關掉 python.exe 與 python3.exe。")
}
Ok "使用 Python $($python.Version)（$($python.Exe)）"

# ---------------------------------------------------------------- Agent 執行檔
#
# 主控台產生安裝包時只重組 MSI 外殼（wix build，約 6 秒），不會編譯 C#。所以
# Agent 的原始碼改過之後，一定要有人先跑 agent\build.ps1 產出
# publish\EndpointAgent.exe 與自訂動作 DLL，否則產出來的安裝包裝的還是舊程式。
#
# 以前那個「有人」是你。現在只要原始碼比產出新，這裡就自己重建 —— 少一個
# 「明明改了卻沒生效」的坑。

if (-not $NoPackaging) {
    Step "Agent 執行檔"
    $agentSrc    = Join-Path $srcRoot "agent\src"
    $agentExe    = Join-Path $srcRoot "agent\publish\EndpointAgent.exe"
    $agentBuild  = Join-Path $srcRoot "agent\build.ps1"

    if (-not (Test-Path $agentSrc) -or -not (Test-Path $agentBuild)) {
        # 只複製了產物、沒帶原始碼的部署方式，維持原本行為。
        Info "沒有 Agent 原始碼，沿用現有的 publish\EndpointAgent.exe"
    } else {
        $newest = Get-ChildItem $agentSrc -Recurse -File -Include *.cs, *.csproj `
                    -ErrorAction SilentlyContinue |
                  Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1
        $builtAt = if (Test-Path $agentExe) { (Get-Item $agentExe).LastWriteTimeUtc }
                   else { [datetime]::MinValue }

        if ($newest -and $newest.LastWriteTimeUtc -gt $builtAt) {
            Info "原始碼比 publish\EndpointAgent.exe 新（$($newest.Name)），重新建置…"
            if (Ensure-DotnetSdk) {
                # 開子行程執行：build.ps1 自己設 ErrorActionPreference='Stop'，
                # 而 dotnet 的 stderr 在那個設定下會變成致命錯誤，會把整支安裝
                # 腳本一起帶走。隔離之後最壞只是這一步失敗。
                & powershell -NoProfile -ExecutionPolicy Bypass -File $agentBuild
                if ($LASTEXITCODE -eq 0 -and (Test-Path $agentExe)) {
                    $mb = [math]::Round((Get-Item $agentExe).Length / 1MB, 1)
                    Ok "Agent 已重建（$mb MB）"
                } else {
                    Warn "Agent 建置失敗（代碼 $LASTEXITCODE），將沿用現有的執行檔。"
                    Warn "產生的安裝包會是舊版 Agent。手動排查：cd agent; .\build.ps1"
                }
            } else {
                Warn "沒有 .NET SDK，無法重建 Agent —— 安裝包會是舊版 Agent。"
            }
        } elseif (Test-Path $agentExe) {
            Ok "Agent 執行檔是最新的"
        } else {
            Warn "找不到 publish\EndpointAgent.exe 也沒有可用的原始碼時間戳記。"
        }
    }
}

# ---------------------------------------------------------------- 佈署檔案

Step "佈署程式碼到 $AppDir"
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing -and $existing.State -eq "Running") {
    Info "停止執行中的服務以進行升級"
    Stop-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 2
}

New-Item -ItemType Directory -Force -Path $AppDir, $HomeDir | Out-Null
# /MIR 會鏡射，但 /XD 排除的目錄不會被刪 -> .env 與 instance\ 在升級時安全。
$rc = robocopy $srcServer $AppDir /MIR /NFL /NDL /NJH /NJS /NP `
    /XD ".venv" "instance" "__pycache__" ".pytest_cache" `
    /XF ".env"
if ($LASTEXITCODE -ge 8) { Die "複製程式碼失敗（robocopy 代碼 $LASTEXITCODE）" }
Ok "伺服器程式碼已同步"

# Agent 原始碼。帶著它，主控台才能自己重建 Agent（「安裝包」頁的「重建 Agent
# 程式」），不必為了改一行 C# 就回到伺服器開 PowerShell。
# 排除 bin/obj：那是建置產物，而且自訂動作 DLL 由下面的迴圈單獨複製。
$agentSrcFrom = Join-Path $srcRoot "agent\src"
if (Test-Path $agentSrcFrom) {
    $agentSrcTo = Join-Path $InstallDir "agent\src"
    New-Item -ItemType Directory -Force -Path $agentSrcTo | Out-Null
    robocopy $agentSrcFrom $agentSrcTo /MIR /NFL /NDL /NJH /NJS /NP /XD "bin" "obj" | Out-Null
}

foreach ($rel in @("agent\publish", "agent\packaging", "agent\signing",
                   "agent\src\EndpointAgent.CustomActions\bin\Release\net472", "docs")) {
    $from = Join-Path $srcRoot $rel
    if (Test-Path $from) {
        $to = Join-Path $InstallDir $rel
        New-Item -ItemType Directory -Force -Path $to | Out-Null
        robocopy $from $to /MIR /NFL /NDL /NJH /NJS /NP | Out-Null
    }
}
if (Test-Path (Join-Path $InstallDir "agent\publish\EndpointAgent.exe")) {
    Ok "Agent 素材已同步（主控台可以產生安裝包）"
} else {
    Warn "找不到 agent\publish\EndpointAgent.exe —— 先在 agent\ 執行 .\build.ps1，再重跑本腳本"
}

Step "建立虛擬環境並安裝相依套件"
$venvOk = (Test-Path $VenvPy) -and (& $VenvPy -m pip --version 2>$null)
if (-not $venvOk) {
    Remove-Item (Join-Path $AppDir ".venv") -Recurse -Force -ErrorAction SilentlyContinue
    & $python.Exe @($python.Args) -m venv (Join-Path $AppDir ".venv")
    if (-not (Test-Path $VenvPy)) { Die "建立虛擬環境失敗。" }
}
& $VenvPy -m pip install --quiet --upgrade pip
& $VenvPy -m pip install --quiet -r (Join-Path $AppDir "requirements.txt")
if ($LASTEXITCODE -ne 0) { Die "安裝相依套件失敗。" }
Ok "相依套件安裝完成"

# ---------------------------------------------------------------- TLS 憑證

Step "TLS 憑證"
$tlsDir  = Join-Path $AppDir "instance\tls"
$certPem = Join-Path $tlsDir "cert.pem"
$keyPem  = Join-Path $tlsDir "key.pem"
New-Item -ItemType Directory -Force -Path $tlsDir | Out-Null

if (Test-Path $certPem) {
    Ok "沿用既有憑證（$tlsDir）"
    $certKind = "existing"
} else {
    $hostName = [System.Net.Dns]::GetHostName()
    $ips = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
             Where-Object { $_.IPAddress -ne "127.0.0.1" -and $_.PrefixOrigin -ne "WellKnown" } |
             Select-Object -ExpandProperty IPAddress -Unique)
    # 憑證與私鑰都用虛擬環境裡的 cryptography 產生／轉換，不需要 OpenSSL。
    $pyScript = Join-Path $env:TEMP "eem-cert.py"
    @'
import datetime, ipaddress, os, sys
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

cert_path, key_path, host = sys.argv[1], sys.argv[2], sys.argv[3]
addresses = [a for a in sys.argv[4:] if a]
# 選配的 PFX 走環境變數：PowerShell 呼叫原生程式時會「吃掉」空字串參數，
# 位置參數會因此錯位；密碼也不該出現在命令列（會被 tasklist 看到）。
pfx = os.environ.get("EEM_CERT_PFX", "")
pfx_pw = os.environ.get("EEM_CERT_PFX_PASSWORD", "")

if pfx:
    from cryptography.hazmat.primitives.serialization import pkcs12
    with open(pfx, "rb") as fh:
        key, cert, extra = pkcs12.load_key_and_certificates(
            fh.read(), pfx_pw.encode() if pfx_pw else None)
    chain = cert.public_bytes(serialization.Encoding.PEM)
    for ca in (extra or []):
        chain += ca.public_bytes(serialization.Encoding.PEM)
    open(cert_path, "wb").write(chain)
else:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    names = [x509.DNSName(host), x509.DNSName("localhost")]
    for a in addresses:
        try:
            names.append(x509.IPAddress(ipaddress.ip_address(a)))
        except ValueError:
            pass
    names.append(x509.IPAddress(ipaddress.ip_address("127.0.0.1")))
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(subject).issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=825))
            .add_extension(x509.SubjectAlternativeName(names), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    open(cert_path, "wb").write(cert.public_bytes(serialization.Encoding.PEM))

open(key_path, "wb").write(key.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption()))
os.chmod(key_path, 0o600)
print("ok")
'@ | Set-Content -Path $pyScript -Encoding UTF8

    $certArgs = @($certPem, $keyPem, $hostName) + $ips
    if ($CertPath) { $env:EEM_CERT_PFX = $CertPath }
    if ($CertPassword) { $env:EEM_CERT_PFX_PASSWORD = $CertPassword }
    try {
        & $VenvPy $pyScript @certArgs | Out-Null
    } finally {
        Remove-Item Env:\EEM_CERT_PFX, Env:\EEM_CERT_PFX_PASSWORD -ErrorAction SilentlyContinue
        Remove-Item $pyScript -Force -ErrorAction SilentlyContinue
    }
    if (-not (Test-Path $certPem)) { Die "產生憑證失敗。" }
    $certKind = if ($CertPath) { "supplied" } else { "self-signed" }
    if ($CertPath) { Ok "已匯入你的憑證（$CertPath）" }
    else { Ok "已產生自簽憑證（CN=$hostName，SAN 含 $($ips -join ', ')）" }
}

# 私鑰只給 SYSTEM 與系統管理員讀
icacls $keyPem /inheritance:r /grant "SYSTEM:(R)" "Administrators:(R)" | Out-Null

# ---------------------------------------------------------------- 設定

Step "設定環境檔 .env"
$envPath = Join-Path $AppDir ".env"
if (Test-Path $envPath) {
    Ok "沿用既有 .env（升級不會動它）"
} else {
    $secret = & $VenvPy -c "import secrets;print(secrets.token_urlsafe(64))"
    @"
# 由 install.ps1 產生。此檔含 JWT 主密鑰，請納入備份並妥善保護。
# 完整選項見 .env.example 與 app\config.py。

EEM_SECRET_KEY=$secret
EEM_DATABASE_URI=sqlite:///eem.db
EEM_LOG_LEVEL=INFO

# 本機直接對外提供 HTTPS（沒有反向代理，所以不要信任 X-Forwarded-For）
EEM_BIND_HOST=0.0.0.0
EEM_BIND_PORT=$Port
EEM_TLS_CERT=$certPem
EEM_TLS_KEY=$keyPem
EEM_TRUST_PROXY_HEADERS=0
"@ | Set-Content -Path $envPath -Encoding UTF8
    Ok "已產生 .env（含隨機 EEM_SECRET_KEY）"
}
icacls $envPath /inheritance:r /grant "SYSTEM:(R)" "Administrators:(F)" | Out-Null

# ---------------------------------------------------------------- 資料庫與管理員

function Invoke-Flask {
    param([string[]]$FlaskArgs, [string]$StdIn)
    Push-Location $AppDir
    # Flask 把 INFO 記錄寫到 stderr。PowerShell 5.1 把原生程式的 stderr 包成
    # ErrorRecord，在 $ErrorActionPreference='Stop' 下會變成致命錯誤，整支腳本
    # 會在「初始化資料庫」無聲中止 —— 所以這段改成 Continue。
    # 再把 ErrorRecord 轉回純字串，否則畫面會出現一整塊紅色的錯誤格式
    # （SilentlyContinue 雖然也不紅，但會連內容一起吃掉，真的失敗時就查不到原因）。
    $flatten = { process { if ($_ -is [System.Management.Automation.ErrorRecord]) { $_.ToString() } else { $_ } } }
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        if ($StdIn) { ($StdIn | & $VenvPy -m flask --app wsgi @FlaskArgs 2>&1 | & $flatten | Out-String) }
        else        { (& $VenvPy -m flask --app wsgi @FlaskArgs 2>&1 | & $flatten | Out-String) }
    } finally {
        $ErrorActionPreference = $prev
        Pop-Location
    }
}

Step "初始化資料庫"
$out = Invoke-Flask -FlaskArgs @("init-db")
if ($LASTEXITCODE -ne 0) { $out; Die "建立資料表失敗。" }
Ok "資料表已建立／補齊（instance\eem.db）"

Step "建立第一個最高管理員"
$newPassword = $null
$password = $env:EEM_ADMIN_PASSWORD
if (-not $password) {
    # 密碼政策：至少 12 碼、四類字元中至少三類。這裡固定給大小寫與數字。
    $password = & $VenvPy -c @"
import secrets, string
pools = [string.ascii_lowercase, string.ascii_uppercase, string.digits]
chars = [secrets.choice(p) for p in pools]
alphabet = ''.join(pools)
chars += [secrets.choice(alphabet) for _ in range(17)]
secrets.SystemRandom().shuffle(chars)
print(''.join(chars))
"@
    $newPassword = $password
}
$env:EEM_BOOTSTRAP_SECRET = & $VenvPy -c "import secrets;print(secrets.token_urlsafe(32))"
$out = Invoke-Flask -FlaskArgs @("bootstrap-super-admin", "--username", $AdminUser, "--password-stdin") -StdIn $password
$bootstrapRc = $LASTEXITCODE
Remove-Item Env:\EEM_BOOTSTRAP_SECRET -ErrorAction SilentlyContinue
if ($bootstrapRc -eq 0) {
    Ok "已建立最高管理員 $AdminUser"
} elseif ($out -match "already exists") {
    Ok "已有最高管理員，略過建立"
    $newPassword = $null
} else {
    $out; Die "建立最高管理員失敗。"
}

# ---------------------------------------------------------------- MSI 工具鏈

if (-not $NoPackaging) {
    Step "MSI 建置工具（WiX）"
    $wixDir = Join-Path $InstallDir "tools\wix"
    $wixExe = Join-Path $wixDir "wix.exe"

    # 少了 SDK 就裝不了 WiX，主控台的「安裝包」頁會一直卡在「找不到 WiX 工具」。
    $dotnet = Ensure-DotnetSdk

    if ($dotnet) {
        # 服務以 SYSTEM 執行，找不到使用者層級的 dotnet tool，所以把 wix 裝在安裝目錄下。
        # （不覆寫 USERPROFILE：WiX 不看那個變數，覆寫只會讓下面找不到剛裝好的擴充。）
        $env:DOTNET_CLI_HOME = $HomeDir
        $prevPref = $ErrorActionPreference
        $ErrorActionPreference = "Continue"   # 同 Invoke-Flask：原生程式的 stderr 不是致命錯誤
        try {
            if (-not (Test-Path $wixExe)) {
                & $dotnet tool install --tool-path $wixDir wix --version "5.*" 2>&1 | Out-Null
            }
            if (Test-Path $wixExe) {
                & $wixExe extension add -g WixToolset.Util.wixext/5.0.2 2>&1 | Out-Null
                & $wixExe extension add -g WixToolset.Firewall.wixext/5.0.2 2>&1 | Out-Null
                # WiX 是用 Windows 的使用者設定檔 API 去找 .wix\extensions，**不看**
                # USERPROFILE 環境變數，所以擴充一定會裝進「執行安裝的管理員」設定檔。
                # 服務以 SYSTEM 執行，不複製過去的話建置會失敗：
                #   error WIX0144: The extension 'WixToolset.Util.wixext' could not be found
                $srcWix = Join-Path $env:USERPROFILE ".wix"
                $sysProfileRaw = (Get-ItemProperty `
                    "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList\S-1-5-18" `
                    -ErrorAction SilentlyContinue).ProfileImagePath
                if ($sysProfileRaw -and (Test-Path $srcWix)) {
                    $sysProfile = [Environment]::ExpandEnvironmentVariables($sysProfileRaw)
                    robocopy $srcWix (Join-Path $sysProfile ".wix") /MIR /NFL /NDL /NJH /NJS /NP | Out-Null
                    Ok "WiX 5 與擴充已就緒（服務帳號可用）"
                } else {
                    Warn "WiX 擴充複製到 SYSTEM 設定檔失敗，主控台建置安裝包時可能會找不到擴充。"
                }
            } else {
                Warn "WiX 安裝失敗，主控台的「安裝包」頁會顯示缺少工具。"
            }
        } finally {
            $ErrorActionPreference = $prevPref
        }
    } else {
        Warn "沒有 .NET SDK 且自動安裝失敗，略過 WiX —— 主控台的「安裝包」頁會顯示缺少工具。"
        Warn "請自行安裝 .NET SDK（https://dotnet.microsoft.com/download）後重跑本腳本。"
    }
    if (Test-Path $wixExe) {
        $envText = Get-Content $envPath -Raw
        if ($envText -notmatch "EEM_WIX_COMMAND=") {
            Add-Content -Path $envPath -Value "EEM_WIX_COMMAND=$wixExe" -Encoding UTF8
        }
    }
}

# ---------------------------------------------------------------- 服務

Step "註冊開機自動啟動"
# 用排程工作而不是 Windows 服務：python.exe 不會回應服務控制管理員，
# 註冊成服務會啟動失敗；排程工作（開機觸發、SYSTEM、失敗自動重啟）行為等價。
$runner = Join-Path $AppDir "run-server.cmd"
@"
@echo off
rem 由 install.ps1 產生。排程工作 $TaskName 會呼叫這支。
rem 不要覆寫 USERPROFILE：WiX 是用設定檔 API 找擴充套件的，覆寫了反而找不到。
set "DOTNET_CLI_HOME=$HomeDir"
set "PYTHONUNBUFFERED=1"
cd /d "$AppDir"
"$VenvPy" wsgi.py >> "$AppDir\instance\server.log" 2>&1
"@ | Set-Content -Path $runner -Encoding OEM

$action    = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$runner`"" -WorkingDirectory $AppDir
$trigger   = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
                -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 `
                -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Description "螢幕測錄系統 管理伺服器" -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Ok "排程工作 $TaskName 已註冊並啟動（開機自動執行）"

if (-not $NoFirewall) {
    if (-not (Get-NetFirewallRule -DisplayName $FirewallRule -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -DisplayName $FirewallRule -Direction Inbound -Protocol TCP `
            -LocalPort $Port -Action Allow -Profile Any | Out-Null
    }
    Ok "防火牆已放行 TCP $Port"
}

# ---------------------------------------------------------------- 驗證

Step "驗證伺服器"
Add-Type -TypeDefinition @"
using System.Net;
using System.Security.Cryptography.X509Certificates;
public class EemCertBypass {
    public static void Enable() {
        ServicePointManager.ServerCertificateValidationCallback = delegate { return true; };
        ServicePointManager.SecurityProtocol = SecurityProtocolType.Tls12;
    }
}
"@ -ErrorAction SilentlyContinue
[EemCertBypass]::Enable()

$healthy = $false
foreach ($i in 1..40) {
    try {
        $r = Invoke-WebRequest -Uri "https://127.0.0.1:$Port/api/health" -UseBasicParsing -TimeoutSec 5
        if ($r.Content -match '"ok"') { $healthy = $true; break }
    } catch { Start-Sleep -Seconds 1 }
}
if ($healthy) {
    Ok "健康檢查通過：https://127.0.0.1:$Port/api/health"
} else {
    Warn "伺服器沒有在 40 秒內回應。記錄檔最後 20 行："
    Get-Content (Join-Path $AppDir "instance\server.log") -Tail 20 -ErrorAction SilentlyContinue
    Die "啟動失敗。修正後可直接重跑本腳本。"
}

Step "部署前檢查（flask check-config）"
Invoke-Flask -FlaskArgs @("check-config") | ForEach-Object { Write-Host "   $_" }

# ---------------------------------------------------------------- 總結

$ipList = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
            Where-Object { $_.IPAddress -ne "127.0.0.1" -and $_.PrefixOrigin -ne "WellKnown" } |
            Select-Object -ExpandProperty IPAddress -Unique)
$portSuffix = if ($Port -eq 443) { "" } else { ":$Port" }

Write-Host ""
Write-Host "══════════════════════════════════════════════════════════" -ForegroundColor Green
Write-Host " 安裝完成" -ForegroundColor White
Write-Host "══════════════════════════════════════════════════════════" -ForegroundColor Green
Write-Host ""
Write-Host "  主控台網址   https://localhost$portSuffix"
foreach ($ip in $ipList) { Write-Host "               https://$ip$portSuffix" }
if ($newPassword) {
    Write-Host "  管理員帳號   $AdminUser"
    Write-Host "  管理員密碼   $newPassword   " -NoNewline
    Write-Host "← 只顯示這一次，請立刻保存" -ForegroundColor Yellow
} else {
    Write-Host "  管理員帳號   $AdminUser（沿用既有密碼）"
}
Write-Host ""
Write-Host "  安裝位置     $AppDir"
Write-Host "  設定檔       $envPath"
Write-Host "  資料與金鑰   $AppDir\instance\（eem.db、recording.key、tls\、錄影、截圖）"
Write-Host "  記錄檔       $AppDir\instance\server.log"
Write-Host ""
Write-Host "  重新啟動     Restart-ScheduledTask -TaskName $TaskName"
Write-Host "  停止         Stop-ScheduledTask -TaskName $TaskName"
Write-Host "  升級／修復   重跑本腳本（資料會保留）"
Write-Host "  解除安裝     .\install.ps1 -Uninstall"
if ($certKind -eq "self-signed") {
    Write-Host ""
    Write-Host "  憑證是自簽的" -ForegroundColor Yellow -NoNewline
    Write-Host "：瀏覽器會警告，且 Agent 的 wss 連線會因不受信任而失敗。"
    Write-Host "  請把 $certPem 匯入端點的「受信任的根憑證授權」（可用 GPO 派送），"
    Write-Host "  或改用 -CertPath 帶入公司憑證重跑。"
}
Write-Host ""
Write-Host "  請備份 $envPath 與 $AppDir\instance\ —— 遺失金鑰＝既有錄影無法解密。" -ForegroundColor Yellow
Write-Host ""
