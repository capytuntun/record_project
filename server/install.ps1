<#
.SYNOPSIS
    螢幕測錄系統（管理伺服器）—— Windows 一鍵安裝。

.DESCRIPTION
    以系統管理員身分執行一行就好：

        powershell -ExecutionPolicy Bypass -File server\install.ps1

    做完這些事：Python 虛擬環境 → 隨機密鑰 .env → 自簽 TLS 憑證 → 建資料庫 →
    建第一個最高管理員 → 開機自動啟動的排程工作 → 防火牆規則 → .NET SDK + WiX
    （讓主控台可以直接產生 MSI 安裝包）→ FFmpeg（螢幕錄影用）。

    缺 Python 3.11+ 或 .NET SDK 時會用 winget 自動補齊，不需要事先手動安裝；
    FFmpeg 則直接下載官方建置版並核對 SHA-256。

    和 Linux 版（install.sh）的差別：Windows 單機不另外架反向代理，**由程式自己
    終結 TLS**（0.0.0.0:443），所以裝完就能用 https:// 連。

    重跑 = 就地升級：程式碼換新，.env 與 instance\（資料庫、金鑰、錄影）保留。

.PARAMETER InstallDir
    安裝位置，預設 C:\EEM。

.PARAMETER Port
    對外的 HTTPS 埠，預設 443。

.PARAMETER CertPath
    自己的憑證（.pfx）。不給就產生自簽憑證。

.PARAMETER FfmpegPath
    指定一個現成的 ffmpeg.exe（離線環境用）。不給就先找本機現有的，找不到再下載。
    必須是含 libx264 的靜態建置版（例如 gyan.dev 的 essentials）。

.PARAMETER NoRecording
    略過 FFmpeg。不裝 FFmpeg 時伺服器照常運作，只是不會錄影。

.PARAMETER ResultFile
    安裝完成後把結果（網址、管理員帳號、「只顯示一次」的密碼、是否可錄影等）寫成
    一個 INI 檔。給圖形安裝精靈（server\packaging\EEMServer.iss）用，一般手動安裝
    不需要。密碼只在本次新建帳號時才有；精靈讀完會立刻刪除這個檔。

.PARAMETER Uninstall
    移除排程工作、相關行程與防火牆規則（資料保留）。加 -Purge 連同整個安裝目錄
    一併刪除（會要求輸入 DELETE 確認，加 -Force 略過）。完全移除建議直接用
    uninstall.ps1。

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
    [string]$FfmpegPath,
    [switch]$NoRecording,
    [switch]$NoFirewall,
    [string]$ResultFile,
    [switch]$Uninstall,
    [switch]$Purge,
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

# 輸出被導向時（存成記錄檔、派送工具）一律寫 UTF-8：預設的 cp950 寫不出 ✓ ▶，
# dotnet 寫進管道的 UTF-8 也會被當成 cp950 解成亂碼。Python 子行程跟著改用
# UTF-8，Invoke-Flask 讀回來才對得上。直接在視窗裡執行時不動。
if ([Console]::IsOutputRedirected) {
    try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false } catch { }
    $env:PYTHONIOENCODING = "utf-8"
}

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

function Test-UnderDir {
    param([string]$Path, [string]$Dir)
    $root = $Dir.TrimEnd('\') + '\'
    return $Path -and ($Path + '\').StartsWith($root, [StringComparison]::OrdinalIgnoreCase)
}

function Read-EnvFile {
    # 讀 .env 的 EEM_* 設定成 hashtable；檔案不在就回空的。
    param([string]$Path)
    $vals = @{}
    if (-not (Test-Path -LiteralPath $Path)) { return $vals }
    foreach ($line in (Get-Content -LiteralPath $Path -ErrorAction SilentlyContinue)) {
        if ($line -match '^\s*(EEM_[A-Z_]+)\s*=\s*(.*?)\s*$') { $vals[$Matches[1]] = $Matches[2].Trim('"') }
    }
    return $vals
}

function Get-ExternalData {
    # .env 可以把資料指到安裝目錄以外。那些位置是使用者自己選的（可能是整顆
    # 資料碟或共用資料夾），不替他刪，只在最後列出來。
    param([string]$Root)
    $notes = @()
    $envFile = Join-Path $Root "server\.env"
    if (-not (Test-Path -LiteralPath $envFile)) { return $notes }
    $vals = Read-EnvFile $envFile
    foreach ($k in @("EEM_RECORDING_DIR", "EEM_SCREENSHOT_DIR", "EEM_PACKAGE_OUTPUT_DIR")) {
        $p = $vals[$k]
        if ($p -and [IO.Path]::IsPathRooted($p) -and -not (Test-UnderDir $p $Root)) {
            $notes += "$k=$p（在安裝目錄外，沒有刪除）"
        }
    }
    $db = $vals["EEM_DATABASE_URI"]
    if ($db -and $db -notmatch '^sqlite:') {
        $notes += "資料庫 $($db -replace '//[^@/]*@', '//***@')（外部資料庫，沒有刪除）"
    } elseif ($db -match '^sqlite:///(.+)$' -and [IO.Path]::IsPathRooted($Matches[1]) -and
              -not (Test-UnderDir $Matches[1] $Root)) {
        $notes += "資料庫 $($Matches[1])（在安裝目錄外，沒有刪除）"
    }
    return $notes
}

function Stop-InstallProcesses {
    # 排程工作只是起點：cmd → python，python 底下還有錄影中的 ffmpeg、建安裝包中的
    # wix／dotnet。任何一個活著，它開著的檔案就刪不掉。以「執行檔或命令列落在安裝
    # 目錄下」認定，但跳過自己與祖先行程 —— 從 C:\EEM\server 執行本腳本時，自己的
    # 命令列也含安裝目錄。
    param([string]$Root)
    $all = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
    $keep = @{}
    $cur = [int]$PID
    while ($cur -and -not $keep.ContainsKey($cur)) {
        $keep[$cur] = $true
        $parent = $all | Where-Object { [int]$_.ProcessId -eq $cur } | Select-Object -First 1
        $cur = if ($parent) { [int]$parent.ParentProcessId } else { 0 }
    }
    $victims = @($all | Where-Object {
        -not $keep.ContainsKey([int]$_.ProcessId) -and (
            (Test-UnderDir $_.ExecutablePath $Root) -or
            ($_.CommandLine -and $_.CommandLine.IndexOf($Root.TrimEnd('\') + '\',
                [StringComparison]::OrdinalIgnoreCase) -ge 0))
    })
    foreach ($p in $victims) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
    if ($victims) {
        Ok "已結束 $($victims.Count) 個相關行程（$((@($victims.Name) | Select-Object -Unique) -join '、')）"
        Start-Sleep -Seconds 2   # 讓系統釋放檔案控制代碼
    }
}

function Repair-Acl {
    # 安裝時把 tls\key.pem 的繼承權限拿掉、連系統管理員都只給讀取（保護私鑰），
    # Remove-Item -Force 要先改檔案屬性，沒有寫入權限就刪不掉。先取得擁有權
    # （擁有者一定能改權限），再把權限恢復成從上層繼承，就能正常刪除。
    # 由上而下處理，子項目才會繼承到已修好的上層權限。
    param([string]$Path)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"   # 同 Invoke-Flask：原生程式的 stderr 不是致命錯誤
    try {
        $items = @(Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue) +
                 @(Get-ChildItem -LiteralPath $Path -Recurse -Force -ErrorAction SilentlyContinue)
        foreach ($it in $items) {
            & takeown.exe /F $it.FullName /A 2>&1 | Out-Null
            & icacls.exe $it.FullName /reset /C /Q 2>&1 | Out-Null
        }
    } finally {
        $ErrorActionPreference = $prev
    }
}

function Remove-Installation {
    $root = [IO.Path]::GetFullPath($InstallDir).TrimEnd('\')
    $external = @()

    if ($Purge) {
        # 擋掉打錯 -InstallDir 的災難：相對路徑（"C:" 指的是目前目錄）、磁碟根目錄、
        # 原始碼資料夾（它也有 server\），或看起來不是本系統的目錄都不刪。
        if ($InstallDir -notmatch '^([A-Za-z]:[\\/]|\\\\)') {
            Die "-InstallDir 必須是完整路徑（例如 C:\EEM），收到的是 '$InstallDir'。"
        }
        if ([IO.Path]::GetPathRoot($root).TrimEnd('\') -eq $root) {
            Die "拒絕刪除磁碟根目錄 $root。請確認 -InstallDir。"
        }
        if (Test-Path -LiteralPath (Join-Path $root ".git")) {
            Die "$root 是原始碼資料夾（有 .git），不是安裝目錄，不刪除。"
        }
        if ((Test-Path -LiteralPath $root) -and -not (Test-Path -LiteralPath (Join-Path $root "server"))) {
            Die "$root 底下沒有 server\，看起來不是螢幕測錄系統的安裝目錄，不刪除。"
        }
        $external = @(Get-ExternalData $root)
        if (-not $Force) {
            Write-Host ""
            Write-Host "將永久刪除 $root，包含：" -ForegroundColor Yellow
            Write-Host "  資料庫、所有錄影與截圖、畫面加密金鑰、TLS 憑證與私鑰、.env、FFmpeg、WiX" -ForegroundColor Yellow
            Write-Host "刪除後無法復原；要保留錄影請先備份 $root\server\instance\。" -ForegroundColor Yellow
            try { $answer = Read-Host "確定要刪除請輸入 DELETE" }
            catch { Die "無法互動確認。確定要刪除請加 -Force。" }
            if ($answer -cne "DELETE") { Write-Host "已取消，沒有變更任何東西。"; exit 1 }
        }
    }

    Step "停止並移除服務"
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($task) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Ok "已停止並移除排程工作 $TaskName"
    } else {
        Info "沒有找到排程工作 $TaskName"
    }
    Stop-InstallProcesses $root

    if (Get-NetFirewallRule -DisplayName $FirewallRule -ErrorAction SilentlyContinue) {
        Remove-NetFirewallRule -DisplayName $FirewallRule
        Ok "已移除防火牆規則「$FirewallRule」"
    }

    if (-not $Purge) {
        Info "資料保留在 $root（要一併刪除請改用 .\uninstall.ps1，或加 -Purge）"
        Write-Host "`n解除安裝完成（資料保留）。" -ForegroundColor Green
        return
    }

    Step "刪除檔案"
    # 安裝時把 WiX 擴充複製進 SYSTEM 的設定檔（服務帳號找擴充用），那份只有本系統在用。
    $sysProfileRaw = (Get-ItemProperty `
        "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList\S-1-5-18" `
        -ErrorAction SilentlyContinue).ProfileImagePath
    if ($sysProfileRaw) {
        $sysWix = Join-Path ([Environment]::ExpandEnvironmentVariables($sysProfileRaw)) ".wix"
        if (Test-Path -LiteralPath $sysWix) {
            Remove-Item -LiteralPath $sysWix -Recurse -Force -ErrorAction SilentlyContinue
            if (Test-Path -LiteralPath $sysWix) { Warn "無法刪除 $sysWix" }
            else { Ok "已刪除服務帳號的 WiX 擴充（$sysWix）" }
        }
    }

    if (Test-Path -LiteralPath $root) {
        # 目前目錄若在安裝目錄裡，那一層會刪不掉。
        Set-Location -LiteralPath $env:TEMP
        for ($i = 1; $i -le 5 -and (Test-Path -LiteralPath $root); $i++) {
            Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue
            if (Test-Path -LiteralPath $root) {
                # 第一次刪不掉多半是權限（見 Repair-Acl），修一次；之後才當成檔案被鎖住在等。
                if ($i -eq 1) { Repair-Acl $root } else { Start-Sleep -Seconds 2 }
            }
        }
        if (Test-Path -LiteralPath $root) {
            $left = @(Get-ChildItem -LiteralPath $root -Recurse -File -Force -ErrorAction SilentlyContinue)
            Warn "$root 沒有刪乾淨（剩 $($left.Count) 個檔案），例如："
            $left | Select-Object -First 5 | ForEach-Object { Info "  $($_.FullName)" }
            Die "可能有程式還開著這些檔案。重新開機後再執行一次 .\uninstall.ps1 即可。"
        }
        Ok "已刪除 $root"
    } else {
        Info "沒有 $root，略過"
    }

    Write-Host "`n伺服器已完全移除。" -ForegroundColor Green
    foreach ($n in $external) { Warn $n }
    Info "保留的共用元件（其他程式可能也在用，需要的話到「設定 → 應用程式」移除）："
    Info "  Python、.NET SDK、$env:USERPROFILE\.wix（WiX 擴充快取）"
    Info "已部署的端點 Agent 不受影響，會持續嘗試連線；請在各端點用 msiexec /x 解除安裝。"
    Info "已拋轉到 FTP／SMB 儲存位置的錄影也不會被刪除。"
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
        # 主控台建安裝包要用解除安裝自訂動作 DLL。安裝檔夾了預建版；萬一缺了（舊安裝檔、
        # 或精簡包），就算 exe 是新的也要補建，否則主控台會卡在「找不到 CA.dll」。
        $caDll = Join-Path $agentSrc "EndpointAgent.CustomActions\bin\Release\net472\EndpointAgent.CustomActions.CA.dll"
        $srcNewer  = $newest -and $newest.LastWriteTimeUtc -gt $builtAt
        $caMissing = -not (Test-Path $caDll)

        if ($srcNewer -or $caMissing) {
            if ($caMissing) { Info "缺少解除安裝自訂動作 DLL，建置 Agent（含 CA.dll）…" }
            else { Info "原始碼比 publish\EndpointAgent.exe 新（$($newest.Name)），重新建置…" }
            if (Ensure-DotnetSdk) {
                # 開子行程執行：build.ps1 自己設 ErrorActionPreference='Stop'，
                # 而 dotnet 的 stderr 在那個設定下會變成致命錯誤，會把整支安裝
                # 腳本一起帶走。隔離之後最壞只是這一步失敗。
                # -SkipWixCheck：WiX 由本腳本稍後的「MSI 建置工具」步驟安裝在
                # tools\wix（不在 PATH），build.ps1 在這時檢查只會誤報找不到。
                #
                # 簽章設定通常寫在 .env（伺服器簽 MSI 用的那份），但 build.ps1
                # 只看環境變數。升級時 .env 已存在，把簽章那幾項借給子行程，
                # 讓 exe 也簽得到；跑完還原，密碼不留在本行程。
                # 已經設成環境變數的優先，不覆寫。
                $envVals = Read-EnvFile (Join-Path $AppDir ".env")
                $borrowed = @()
                foreach ($k in @("EEM_SIGNING_PFX", "EEM_SIGNING_PASSWORD", "EEM_SIGNING_TIMESTAMP_URL")) {
                    if ($envVals[$k] -and -not [Environment]::GetEnvironmentVariable($k)) {
                        [Environment]::SetEnvironmentVariable($k, $envVals[$k])
                        $borrowed += $k
                    }
                }
                # build.ps1 的輸出被接管時寫 UTF-8；本行程的輸出若在 PowerShell 裡被導向
                # （.\install.ps1 *> log.txt），就得用 UTF-8 讀回來，否則一樣是亂碼。
                $prevEncoding = [Console]::OutputEncoding
                try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false } catch { }
                try {
                    & powershell -NoProfile -ExecutionPolicy Bypass -File $agentBuild -SkipWixCheck
                } finally {
                    foreach ($k in $borrowed) { [Environment]::SetEnvironmentVariable($k, $null) }
                    try { [Console]::OutputEncoding = $prevEncoding } catch { }
                }
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
# tools\ 也排除：舊文件教人把 ffmpeg.exe 放在 server\tools\ffmpeg，鏡射會把它刪掉。
$rc = robocopy $srcServer $AppDir /MIR /NFL /NDL /NJH /NJS /NP `
    /XD ".venv" "instance" "__pycache__" ".pytest_cache" "tools" `
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
if ($bootstrapRc -eq 0) {
    Ok "已建立最高管理員 $AdminUser"
} elseif ($out -match "already exists") {
    # 帳號已存在：每次安裝都把密碼重設成本次這一組，讓管理者一定拿得到可登入的
    # 密碼（雜湊無法還原舊密碼）。這會撤銷該帳號目前所有登入階段；資料庫與錄影不動。
    # 沿用同一個 EEM_BOOTSTRAP_SECRET 恢復視窗。
    $out = Invoke-Flask -FlaskArgs @("reset-password", "--username", $AdminUser, "--password-stdin") -StdIn $password
    if ($LASTEXITCODE -ne 0) {
        $out; Remove-Item Env:\EEM_BOOTSTRAP_SECRET -ErrorAction SilentlyContinue
        Die "重設最高管理員密碼失敗。"
    }
    Ok "已重設最高管理員 $AdminUser 的密碼（舊登入階段已失效）"
} else {
    $out; Remove-Item Env:\EEM_BOOTSTRAP_SECRET -ErrorAction SilentlyContinue
    Die "建立最高管理員失敗。"
}
Remove-Item Env:\EEM_BOOTSTRAP_SECRET -ErrorAction SilentlyContinue

# ---------------------------------------------------------------- MSI 工具鏈

if (-not $NoPackaging) {
    Step "MSI 建置工具（WiX）"
    $wixDir = Join-Path $InstallDir "tools\wix"
    $wixExe = Join-Path $wixDir "wix.exe"

    # 少了 SDK 就裝不了 WiX，主控台的「安裝包」頁會一直卡在「找不到 WiX 工具」。
    $dotnet = Ensure-DotnetSdk

    if ($dotnet) {
        # 服務以 SYSTEM 執行，找不到使用者層級的 dotnet tool，所以把 wix 裝在安裝目錄下。
        # （擴充的位置由 WIX_EXTENSIONS 決定，見下面；不要去覆寫 USERPROFILE。）
        $env:DOTNET_CLI_HOME = $HomeDir
        $prevPref = $ErrorActionPreference
        $ErrorActionPreference = "Continue"   # 同 Invoke-Flask：原生程式的 stderr 不是致命錯誤
        try {
            if (-not (Test-Path $wixExe)) {
                & $dotnet tool install --tool-path $wixDir wix --version "5.*" 2>&1 | Out-Null
            }
            if (Test-Path $wixExe) {
                # WiX 5 找擴充的順序：目前目錄\.wix\extensions →「WIX_EXTENSIONS 環境變數，
                # 沒設才用使用者設定檔」\.wix\extensions（WixToolset.Core ExtensionManager）。
                # 服務以 SYSTEM 執行，設定檔跟安裝者不同，曾因此在新機器上一直：
                #   error WIX0144: The extension 'WixToolset.Util.wixext' could not be found
                # 所以安裝與服務都把 WIX_EXTENSIONS 指到安裝目錄下的 home：在這裡下載、從這裡找，
                # 不再碰任何使用者或 SYSTEM 設定檔（.env 與 run-server.cmd 都會設這個變數）。
                # UI 擴充提供安裝精靈（下一步、選安裝位置）的畫面。
                $wixExtIds   = @("WixToolset.Util.wixext/5.0.2",
                                 "WixToolset.Firewall.wixext/5.0.2",
                                 "WixToolset.UI.wixext/5.0.2")
                $wixExtNames = @("WixToolset.Util.wixext",
                                 "WixToolset.Firewall.wixext",
                                 "WixToolset.UI.wixext")
                # 驗證某個 .wix 目錄底下三個擴充的 DLL 都在（直接看檔案，不靠工具的退出碼
                # ——過去就因為「悄悄沒裝成功卻報就緒」導致事後才爆 WIX0144）。
                $HasWixExts = {
                    param([string]$WixDir)
                    $extDir = Join-Path $WixDir "extensions"
                    if (-not (Test-Path $extDir)) { return $false }
                    foreach ($n in $wixExtNames) {
                        $dll = Get-ChildItem -Path (Join-Path $extDir $n) -Recurse -Filter "$n.dll" `
                                   -ErrorAction SilentlyContinue
                        if (-not $dll) { return $false }
                    }
                    return $true
                }

                $env:WIX_EXTENSIONS = $HomeDir
                $homeWix = Join-Path $HomeDir ".wix"
                foreach ($id in $wixExtIds) { & $wixExe extension add -g $id 2>&1 | Out-Null }
                # 下載偶爾會逾時：還缺就再補一次。
                if (-not (& $HasWixExts $homeWix)) {
                    foreach ($id in $wixExtIds) { & $wixExe extension add -g $id 2>&1 | Out-Null }
                }
                # 還是沒有（離線、擋 NuGet）：安裝者自己的設定檔若有現成的擴充（之前裝過），搬過來。
                if (-not (& $HasWixExts $homeWix)) {
                    $userWix = Join-Path $env:USERPROFILE ".wix"
                    if (& $HasWixExts $userWix) {
                        robocopy $userWix $homeWix /E /NFL /NDL /NJH /NJS /NP | Out-Null
                    }
                }

                if (& $HasWixExts $homeWix) {
                    Ok "WiX 5 與擴充已就緒（已確認 $homeWix\extensions 含三個擴充）"
                } else {
                    Warn "WiX 擴充未能裝進 $homeWix\extensions（多半是連不到 NuGet 下載）。"
                    Warn "主控台建安裝包會失敗（WIX0144）。請確認可連網後重跑本腳本，"
                    Warn "或把一台已裝好的機器的 %USERPROFILE%\.wix 整個資料夾複製到 $homeWix 後重啟服務。"
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
        # 服務建安裝包時 wix 從這裡找擴充（wsgi.py 會把 .env 載入環境變數，wix 子行程繼承）。
        if ($envText -notmatch "WIX_EXTENSIONS=") {
            Add-Content -Path $envPath -Value "WIX_EXTENSIONS=$HomeDir" -Encoding UTF8
        }
    }
}

# ---------------------------------------------------------------- FFmpeg（螢幕錄影）
#
# 伺服器把 Agent 傳來的 JPEG 交給 FFmpeg 編成 H.264；沒有 FFmpeg 就不會錄影，
# 主控台「錄影」頁會一直顯示「錄影未啟用」。Windows 沒有系統內建的 FFmpeg，
# 所以這裡備好一份，放在 $InstallDir\tools\ffmpeg（和 WiX 同層，不在升級時會被
# 鏡射覆蓋的 server\ 底下）。app\config.py 預設就找這個位置，不必寫進 .env。

function Test-Ffmpeg {
    # 實際跑一次才算數：檔案在不代表能用（shared 建置版只帶 exe 會缺 DLL），
    # 而錄影用的是 libx264，少了它要等到第一段錄影才會失敗。
    param([string]$Exe)
    if (-not $Exe -or -not (Test-Path -LiteralPath $Exe -PathType Leaf)) { return $false }
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"   # 同 Invoke-Flask：原生程式的 stderr 不是致命錯誤
    try {
        $out = & $Exe -hide_banner -encoders 2>$null | Out-String
        return ($LASTEXITCODE -eq 0) -and ($out -match '\blibx264\b')
    } catch {
        return $false
    } finally {
        $ErrorActionPreference = $prev
    }
}

function Expand-FfmpegZip {
    # 從 zip 只解出 ffmpeg.exe 到 $WorkDir，回傳它的路徑。官方建置版放在 bin\ 底下，
    # repo 內附的 server\tools\ffmpeg\ffmpeg.zip 則放在根目錄，兩種都認。
    param([string]$ZipPath, [string]$WorkDir)
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [IO.Compression.ZipFile]::OpenRead($ZipPath)
    try {
        $entry = $zip.Entries |
                 Where-Object { $_.FullName -match '(^|/)ffmpeg\.exe$' } |
                 Select-Object -First 1
        if (-not $entry) { throw "壓縮檔裡沒有 ffmpeg.exe" }
        $exe = Join-Path $WorkDir "ffmpeg.exe"
        [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $exe, $true)
        return $exe
    } finally {
        $zip.Dispose()
    }
}

function Get-FfmpegDownload {
    # 下載、核對 SHA-256、只解出 bin\ffmpeg.exe。回傳它的路徑，全部來源都失敗回傳 $null。
    # 兩個來源都是 FFmpeg 官網列出的 Windows 建置版，皆為含 libx264 的靜態建置。
    param([string]$WorkDir)
    $sources = @(
        @{ Name = "gyan.dev"
           Zip  = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
           Sha  = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip.sha256"
           File = "" },
        @{ Name = "GitHub BtbN"
           Zip  = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"
           Sha  = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/checksums.sha256"
           File = "ffmpeg-master-latest-win64-gpl.zip" }
    )
    # PowerShell 5.1 不一定預設啟用 TLS 1.2，而兩個來源都要求它。
    [Net.ServicePointManager]::SecurityProtocol =
        [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    Add-Type -AssemblyName System.IO.Compression.FileSystem

    foreach ($src in $sources) {
        $zipPath = Join-Path $WorkDir "ffmpeg.zip"
        try {
            Info "從 $($src.Name) 下載 FFmpeg（約 100 MB）…"
            $shaText = (Invoke-WebRequest -Uri $src.Sha -UseBasicParsing -TimeoutSec 60).Content
            if ($shaText -is [byte[]]) { $shaText = [Text.Encoding]::ASCII.GetString($shaText) }
            # gyan.dev 的 .sha256 只有一個雜湊；BtbN 的是「雜湊  檔名」一檔一行。
            $line = $shaText
            if ($src.File) {
                $line = $shaText -split "`r?`n" |
                        Where-Object { $_ -match ("\s\*?" + [regex]::Escape($src.File) + "\s*$") } |
                        Select-Object -First 1
            }
            if (-not ("$line" -match '([0-9a-fA-F]{64})')) { throw "讀不到 SHA-256 校驗值" }
            $expected = $Matches[1].ToLower()

            Invoke-WebRequest -Uri $src.Zip -OutFile $zipPath -UseBasicParsing -TimeoutSec 900
            $actual = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLower()
            if ($actual -ne $expected) { throw "SHA-256 不符（預期 $expected，實際 $actual）" }

            return Expand-FfmpegZip $zipPath $WorkDir
        } catch {
            Warn "$($src.Name) 下載失敗：$($_.Exception.Message)"
        } finally {
            Remove-Item -LiteralPath $zipPath -Force -ErrorAction SilentlyContinue
        }
    }
    return $null
}

$ffReady = $false
if ($NoRecording) {
    Info "-NoRecording：略過 FFmpeg（伺服器不會錄影）"
} else {
    Step "FFmpeg（螢幕錄影）"
    $ffDir = Join-Path $InstallDir "tools\ffmpeg"
    $ffExe = Join-Path $ffDir "ffmpeg.exe"
    New-Item -ItemType Directory -Force -Path $ffDir | Out-Null

    # 已就位且能用就不動它（錄影中的 ffmpeg.exe 也可能被鎖住，覆寫只會失敗）。
    $ffReady = Test-Ffmpeg $ffExe
    if ($ffReady) {
        Ok "FFmpeg 已就緒（$ffExe）"
    } else {
        # 先用現成的：-FfmpegPath、舊文件說的 server\tools\ffmpeg、PATH 上的。
        $candidates = @()
        if ($FfmpegPath) { $candidates += $FfmpegPath }
        $candidates += (Join-Path $AppDir "tools\ffmpeg\ffmpeg.exe")
        $candidates += (Join-Path $srcServer "tools\ffmpeg\ffmpeg.exe")
        $onPath = Get-Command ffmpeg -ErrorAction SilentlyContinue
        if ($onPath) { $candidates += $onPath.Source }

        foreach ($c in ($candidates | Select-Object -Unique)) {
            if (-not (Test-Ffmpeg $c)) {
                if ($c -eq $FfmpegPath) {
                    Warn "-FfmpegPath 指定的 $c 無法使用（不存在、無法執行，或不含 libx264）"
                }
                continue
            }
            # 複製到服務帳號（SYSTEM）一定讀得到的位置；複製後再驗一次，
            # 因為 shared 建置版離開它的 DLL 就跑不起來。
            Copy-Item -LiteralPath $c -Destination $ffExe -Force
            if (Test-Ffmpeg $ffExe) {
                Ok "使用現有的 FFmpeg（$c → $ffExe）"
                $ffReady = $true
                break
            }
            Warn "$c 複製出來無法單獨執行（可能是需要 DLL 的 shared 建置版），略過"
        }

        # 再來是 repo 內附的 ffmpeg.zip（來源與授權見 server\tools\ffmpeg\SOURCE.md），
        # 不必連網。exe 本身超過 GitHub 單檔 100 MB 上限，所以 repo 裡放的是壓縮檔。
        $bundled = Join-Path $srcServer "tools\ffmpeg\ffmpeg.zip"
        if (-not $ffReady -and -not $FfmpegPath -and (Test-Path -LiteralPath $bundled)) {
            $ffWork = Join-Path $env:TEMP ("eem-ffmpeg-" + [guid]::NewGuid().ToString("N"))
            New-Item -ItemType Directory -Force -Path $ffWork | Out-Null
            try {
                $unpacked = Expand-FfmpegZip $bundled $ffWork
                if (Test-Ffmpeg $unpacked) {
                    Copy-Item -LiteralPath $unpacked -Destination $ffExe -Force
                    $ffReady = Test-Ffmpeg $ffExe
                }
            } catch {
                Warn "內附的 ffmpeg.zip 解壓失敗：$($_.Exception.Message)"
            } finally {
                Remove-Item -LiteralPath $ffWork -Recurse -Force -ErrorAction SilentlyContinue
            }
            if ($ffReady) { Ok "使用 repo 內附的 FFmpeg（$ffExe）" }
            else { Warn "內附的 ffmpeg.zip 無法使用，改為下載" }
        }

        if (-not $ffReady -and -not $FfmpegPath) {
            $ffWork = Join-Path $env:TEMP ("eem-ffmpeg-" + [guid]::NewGuid().ToString("N"))
            New-Item -ItemType Directory -Force -Path $ffWork | Out-Null
            try {
                $downloaded = Get-FfmpegDownload $ffWork
                if ($downloaded -and (Test-Ffmpeg $downloaded)) {
                    Copy-Item -LiteralPath $downloaded -Destination $ffExe -Force
                    $ffReady = Test-Ffmpeg $ffExe
                }
            } finally {
                Remove-Item -LiteralPath $ffWork -Recurse -Force -ErrorAction SilentlyContinue
            }
            if ($ffReady) { Ok "FFmpeg 已下載並驗證（$ffExe）" }
        }
    }

    if (-not $ffReady) {
        # 不留一個壞掉的檔：config.py 只看檔案在不在，留著會讓錄影「看似啟用」卻編碼失敗。
        Remove-Item -LiteralPath $ffExe -Force -ErrorAction SilentlyContinue
        Warn "沒有可用的 FFmpeg —— 伺服器照常運作，但不會錄影（主控台「錄影」頁會顯示「錄影未啟用」）。"
        Warn "離線環境：到 https://www.gyan.dev/ffmpeg/builds/ 下載 essentials 版，"
        Warn "解壓後以 -FfmpegPath <路徑>\bin\ffmpeg.exe 重跑本腳本。"
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
rem WiX 從 WIX_EXTENSIONS\.wix\extensions 找擴充（install.ps1 把擴充裝在這裡）；不要覆寫 USERPROFILE。
set "DOTNET_CLI_HOME=$HomeDir"
set "WIX_EXTENSIONS=$HomeDir"
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
    # 圖形安裝精靈靠結果檔（下面）判斷成敗 —— 這裡先別 Die，讓它寫得出 healthy=0，
    # 精靈才能顯示「未完成、請看記錄檔」而不是把視窗一關就沒有線索。
    if (-not $ResultFile) { Die "啟動失敗。修正後可直接重跑本腳本。" }
    else { Warn "安裝精靈會回報未完成；修正問題後重新執行安裝精靈即可（資料會保留）。" }
}

# ---------------------------------------------------------------- 結果檔（給安裝精靈）
# 只在指定 -ResultFile 時寫。值都是 ASCII（密碼是英數字），用 GetPrivateProfileString
# 讀得到。密碼在本次由安裝程式自動產生時才有值（$newPassword）——不論新建或重設；
# 只有以 EEM_ADMIN_PASSWORD 指定自訂密碼時才留空（你已知道那組密碼）。
if ($ResultFile) {
    $rsuffix    = if ($Port -eq 443) { "" } else { ":$Port" }
    $rpw        = if ($newPassword) { $newPassword } else { "" }
    $rrec       = if ($ffReady) { "1" } else { "0" }
    $rhealthy   = if ($healthy) { "1" } else { "0" }
    $rip        = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
                    Where-Object { $_.IPAddress -ne "127.0.0.1" -and $_.PrefixOrigin -ne "WellKnown" } |
                    Select-Object -ExpandProperty IPAddress -Unique | Select-Object -First 1)
    $rlan       = if ($rip) { "https://$rip$rsuffix" } else { "" }
    try {
        @(
            "[result]"
            "url=https://localhost$rsuffix"
            "lanUrl=$rlan"
            "adminUser=$AdminUser"
            "adminPassword=$rpw"
            "recording=$rrec"
            "certKind=$certKind"
            "healthy=$rhealthy"
            "installDir=$InstallDir"
            "port=$Port"
        ) -join "`r`n" | Set-Content -LiteralPath $ResultFile -Encoding ASCII
    } catch {
        Warn "無法寫入結果檔 $ResultFile：$($_.Exception.Message)"
    }
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
    Write-Host "  管理員帳號   $AdminUser（使用你指定的 EEM_ADMIN_PASSWORD）"
}
Write-Host ""
Write-Host "  安裝位置     $AppDir"
Write-Host "  設定檔       $envPath"
Write-Host "  資料與金鑰   $AppDir\instance\（eem.db、recording.key、tls\、錄影、截圖）"
Write-Host "  記錄檔       $AppDir\instance\server.log"
if ($ffReady) {
    Write-Host "  螢幕錄影     可用（FFmpeg：$InstallDir\tools\ffmpeg\ffmpeg.exe）"
} else {
    Write-Host "  螢幕錄影     " -NoNewline
    Write-Host "未啟用（沒有 FFmpeg，見上方說明）" -ForegroundColor Yellow
}
Write-Host ""
Write-Host "  重新啟動     Restart-ScheduledTask -TaskName $TaskName"
Write-Host "  停止         Stop-ScheduledTask -TaskName $TaskName"
Write-Host "  升級／修復   重跑本腳本（資料會保留）"
Write-Host "  完全移除     .\uninstall.ps1（加 -KeepData 保留資料）"
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
