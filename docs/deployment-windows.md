# 螢幕測錄系統 — Windows 安裝手冊

把管理伺服器裝在 **Windows** 上。Linux 版見 [`deployment.md`](deployment.md)。

---

## 1. 一鍵安裝

**以系統管理員身分**開啟 PowerShell，執行：

```powershell
powershell -ExecutionPolicy Bypass -File server\install.ps1
```

跑完就是可以登入的系統，最後會印出網址與管理員密碼：

```
══════════════════════════════════════════════════════════
 安裝完成
══════════════════════════════════════════════════════════

  主控台網址   https://localhost
               https://192.168.0.55
  管理員帳號   admin
  管理員密碼   b9Ix6pwhEX8km9wmSqD8   ← 只顯示這一次，請立刻保存
```

> **密碼只印這一次**，沒有存成檔案。

和 Linux 版最大的差別：**Windows 版不用另外架反向代理**。程式自己終結 TLS，
直接監聽 `0.0.0.0:443`，安裝時會產生自簽憑證並開好防火牆。

> 真的需要在前面擺 nginx（共用 443、公司規定統一代理、要 nginx 的存取控制）時，
> 見 [`nginx-windows.md`](nginx-windows.md)。注意那不會讓系統變快 —— Windows 版
> nginx 是移植版本，效能反而不如內建的做法。

---

## 2. 該選 Windows 還是 Linux？

| | Windows | Linux |
| --- | --- | --- |
| 主控台、RBAC、稽核、即時畫面、螢幕牆 | ✅ | ✅ |
| 螢幕錄影／截圖、NAS 儲存 | ✅（需 `server\tools\ffmpeg\ffmpeg.exe`） | ✅（`apt install ffmpeg`） |
| **主控台直接產生 MSI 安裝包** | ✅ **可以**（WiX 只支援 Windows） | ❌ 要另外找一台 Windows 建（見 `deployment.md` §8.1） |
| MSI 簽章 | ✅ | ❌ |
| HTTPS | 程式自己終結 TLS（可改用 nginx，見 [`nginx-windows.md`](nginx-windows.md)） | nginx 終結 TLS |
| 開機自動啟動 | 排程工作（SYSTEM） | systemd |

**如果你會頻繁產生安裝包，就裝 Windows 版**——這是它唯一但關鍵的優勢。

---

## 3. 需求

| 項目 | 說明 |
| --- | --- |
| 作業系統 | Windows 10／11／Server 2019 以上 |
| 權限 | 系統管理員（UAC） |
| Python | **3.11 以上**；沒有的話腳本會試著用 winget 裝 |
| .NET SDK | 只有「要在主控台建 MSI」才需要（WiX 與 Agent 執行檔都靠它）；沒有的話腳本會用 winget 裝 |
| FFmpeg | 只有「螢幕錄影」需要，放在 `server\tools\ffmpeg\ffmpeg.exe` |

安裝完的樣子：

```
C:\EEM\
├── server\             ← 程式碼
│   ├── .venv\          ← 虛擬環境
│   ├── .env            ← 密鑰與設定（只有 SYSTEM 與 Administrators 讀得到）
│   ├── run-server.cmd  ← 排程工作呼叫的啟動器
│   └── instance\       ← 資料庫、加密金鑰、TLS 憑證、錄影、截圖、安裝包、server.log
├── agent\              ← Agent 產物（產生 MSI 用）
├── tools\wix\          ← WiX 5（給服務帳號用）
├── home\               ← 服務行程的 dotnet 家目錄
└── docs\
```

---

## 4. 安裝程式做了什麼

| 步驟 | 內容 |
| --- | --- |
| 1. Python | 找 3.11+ 的直譯器（`py -3.13/-3.12/-3.11`、`python`），必要時用 winget 安裝 |
| 2. Agent 執行檔 | `agent\src\` 的 `.cs`／`.csproj` 比 `agent\publish\EndpointAgent.exe` 新就自動跑 `agent\build.ps1` 重建（見下方註） |
| 3. 佈署 | `robocopy` 到 `C:\EEM\server`，一併複製 `agent\` 產物與 `docs\` |
| 4. 虛擬環境 | 建 `.venv` 並安裝 `requirements.txt` |
| 5. TLS 憑證 | 產生自簽憑證（SAN 含主機名與所有本機 IP），私鑰 ACL 只給 SYSTEM／Administrators |
| 6. `.env` | 隨機 `EEM_SECRET_KEY`、`EEM_BIND_HOST=0.0.0.0`、`EEM_BIND_PORT=443`、TLS 路徑 |
| 7. 資料庫 | `flask init-db` |
| 8. 最高管理員 | `flask bootstrap-super-admin`，密碼隨機產生（或用環境變數 `EEM_ADMIN_PASSWORD`） |
| 9. WiX | 缺 .NET SDK 就先用 winget 裝（`Microsoft.DotNet.SDK.9`），再把 WiX 5 裝到 `C:\EEM\tools\wix`，並把擴充複製進 SYSTEM 的設定檔（見下方註） |
| 10. 開機啟動 | 註冊排程工作 `EEMManagementServer`（開機觸發、SYSTEM、失敗自動重啟 3 次） |
| 11. 防火牆 | 放行 TCP 443 |
| 12. 驗證 | 等 `https://127.0.0.1/api/health` 回 `{"status":"ok"}`，再跑 `flask check-config` |

四個實作上的坑，安裝程式已經處理掉：

- **主控台產包不會編譯 C#**：產生安裝包時只重組 MSI 外殼（`wix build`，約 6 秒），
  用的是 `agent\publish\EndpointAgent.exe` 這個**事先建好**的執行檔。這是刻意的
  —— 每台端點的 Agent 執行檔完全相同（只有設定檔不同），每產一包都重編是浪費
  分鐘級的時間。代價是「改了 Agent 原始碼卻忘了重建」會裝到舊程式而且毫無徵兆，
  所以第 2 步會比對時間戳記並自動重建。手動等價指令是 `cd agent; .\build.ps1`。

- **為什麼用排程工作而不是 Windows 服務**：`python.exe` 不會回應服務控制管理員
  （SCM），用 `sc create` 註冊成服務會啟動失敗。排程工作以「開機觸發 + SYSTEM +
  失敗重啟」達成同樣效果，而且不需要 NSSM 之類的外部工具。
- **WiX 擴充要複製到 SYSTEM 設定檔**：WiX 是用 Windows 的使用者設定檔 API 找
  `.wix\extensions`，**不看 `USERPROFILE` 環境變數**。擴充會裝進「執行安裝的管理員」
  設定檔，而服務以 SYSTEM 執行 —— 沒複製過去的話建置會失敗：
  `error WIX0144: The extension 'WixToolset.Util.wixext' could not be found`。
- **有 `dotnet.exe` 不代表有 SDK**：只裝執行階段（runtime）時 `dotnet.exe` 一樣在，
  但 `dotnet tool install` 需要 SDK。腳本是用 `dotnet --list-sdks` 有沒有輸出來判斷，
  不是看命令存不存在。

---

## 5. 選項

```powershell
-InstallDir <路徑>      安裝位置，預設 C:\EEM
-Port <埠>              HTTPS 埠，預設 443
-AdminUser <帳號>       第一個最高管理員，預設 admin
-CertPath <pfx路徑>     用自己的憑證取代自簽（搭配 -CertPassword）
-NoPackaging            不裝 .NET SDK 與 WiX（不需要在這台建安裝包）
-NoFirewall             不動防火牆
-Uninstall              移除排程工作與防火牆規則（資料保留）
-Uninstall -Purge       連同 C:\EEM 一併刪除
```

無人值守安裝可先設環境變數 `EEM_ADMIN_PASSWORD` 指定密碼（政策：至少 12 碼，
大寫／小寫／數字／符號四類中至少三類）。

---

## 6. 安裝後

### 服務控制

```powershell
Get-ScheduledTask -TaskName EEMManagementServer          # 狀態
Restart-ScheduledTask -TaskName EEMManagementServer      # 重啟（改完 .env 要重啟）
Stop-ScheduledTask -TaskName EEMManagementServer         # 停止
Get-Content C:\EEM\server\instance\server.log -Tail 30   # 記錄
```

### 升級

**重跑同一支腳本**，`.env` 與 `instance\`（資料庫、金鑰、錄影、憑證）都會保留：

```powershell
powershell -ExecutionPolicy Bypass -File server\install.ps1
```

### 解除安裝

```powershell
.\install.ps1 -Uninstall            # 留資料
.\install.ps1 -Uninstall -Purge     # 全部刪掉
```

---

## 7. TLS 憑證

預設是**自簽憑證**（`C:\EEM\server\instance\tls\`），SAN 含主機名與所有本機 IP。

> ⚠️ 自簽憑證瀏覽器會警告，而且 **Agent 的 `wss` 連線會因憑證不受信任而失敗**
> ——端點會一直顯示離線。瀏覽器的警告可以按「繼續前往」略過，**Agent 不行**：
> 它刻意不繞過 TLS 驗證（CLAUDE.md §30），所以端點必須真的信任這張憑證。

想要一勞永逸不再處理憑證，就用公司憑證重裝：

```powershell
.\install.ps1 -CertPath C:\certs\eem.pfx -CertPassword 'pfx密碼'
```

（憑證密碼只透過環境變數傳給轉檔程式，不會出現在命令列。）
否則就讓端點信任這張自簽憑證 —— 見下面三種方式。

### 7.1 安裝包自動夾帶（預設，不需要 GPO）

**安裝包產生時會把伺服器的憑證一起包進 MSI**，安裝時自動寫入端點的
「受信任的根憑證授權」，解除安裝時移除。端點不必做任何事。

主控台的「安裝包」頁會直接告訴你這台伺服器目前會不會夾帶。夾帶的來源：

| 設定 | 說明 |
| --- | --- |
| `EEM_TLS_CERT` | 預設 —— 程式自己終結 TLS 時就是這張（`install.ps1` 會設） |
| `EEM_PACKAGE_CA_CERT` | 覆寫。**在 nginx 後面時要設這個** —— 那時 `EEM_TLS_CERT` 是空的，要指向 nginx 用的憑證 |

實作要點：

* 夾帶的是伺服器的**葉憑證**，不是簽發它的 CA。這張憑證的 BasicConstraints 是
  `CA:FALSE`，所以就算被當成信任錨，它也只能為**自己**背書，無法為任何其他網域
  簽出可信憑證。這和「在端點安裝一個 CA」是完全不同量級的授權。
* 憑證安裝失敗不會讓整個安裝失敗，也不會擋住解除安裝（自訂動作 `Return="ignore"`）。
* 解除安裝時以 **SHA-1 指紋**移除，不依賴檔案還在不在。
* 大版本升級時不移除信任（新版會重新加上同一張）。
* 伺服器換憑證後，**舊安裝包會失效** —— 要重新產一包給新端點；已安裝的端點得
  用 §7.3 手動更新，或重裝 Agent。

### 7.2 怎麼判斷就是這個問題

在**端點**上跑（`curl.exe` 是 Windows 10 以上內建，注意要寫 `.exe`）：

```powershell
Test-NetConnection <伺服器IP> -Port 443       # TcpTestSucceeded 要是 True
curl.exe    https://<伺服器IP>/api/health     # 不加 -k
curl.exe -k https://<伺服器IP>/api/health     # 加 -k 繞過驗證
```

| 結果 | 判讀 |
| --- | --- |
| `TcpTestSucceeded : False` | 網路層 —— 兩台不在同一網段（VMware NAT／host-only／橋接混用）或防火牆 |
| 不加 `-k` 出現 **`SEC_E_UNTRUSTED_ROOT`**，加 `-k` 回 `{"status":"ok"}` | **就是憑證不受信任** —— 照 §7.3 匯入 |
| 兩個都成功 | 憑證沒問題，改查註冊憑證是否過期／撤銷／次數用完（事件記錄檔） |

Agent 的症狀是托盤顯示「註冊狀態：尚未註冊」、主控台端點清單空的。

### 7.3 手動匯入（舊安裝包、或憑證換過的既有端點）

正式環境請用 GPO 派送。少量機器可以直接在**端點**上跑，向伺服器取得它實際提供的
憑證再匯入，不必搬檔案：

```powershell
# 系統管理員 PowerShell
$url = "https://192.168.228.173"        # 換成你的伺服器網址
[Net.ServicePointManager]::ServerCertificateValidationCallback = { $true }
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$req = [Net.HttpWebRequest]::Create("$url/api/health")
$req.GetResponse().Close()
$cert = $req.ServicePoint.Certificate
[IO.File]::WriteAllBytes("$env:TEMP\eem.cer", $cert.Export('Cert'))

certutil -addstore -f Root "$env:TEMP\eem.cer"
Restart-Service EndpointAgent
```

> ⚠️ 這是**首次使用即信任**：抓的是當下網路上那台伺服器給的憑證，中間人可以偽造。
> 實驗室內網可以接受；正式部署請把 `cert.pem` 透過可信管道（GPO、共用磁碟、
> 隨身碟）送過去再 `certutil -addstore -f Root cert.pem`，不要用上面這段抓。

驗證成功的樣子 —— **不加** `-k` 也要回 `{"status":"ok"}`：

```powershell
curl.exe https://192.168.228.173/api/health
```

若改成出現 `CERT_E_CN_NO_MATCH`，代表信任已經建立，但**憑證的 SAN 不含這個 IP**
——伺服器裝好之後 IP 變了。到伺服器上刪掉 `C:\EEM\server\instance\tls\cert.pem`
與 `key.pem` 再重跑 `install.ps1`，它會用目前的 IP 重新產生憑證（端點要重新匯入）。

---

## 8. 產生端點安裝包（這裡可以直接做）

主控台「安裝包」頁 → 填標籤、伺服器網址、有效期間、使用次數、管理密碼 → 建立，
伺服器會在約 10 秒內建好 MSI 並提供下載。端點安裝：

```powershell
msiexec /i <下載的>.msi /qn
```

**伺服器網址要填端點連得到的位址**（例：`https://192.168.0.55`），不要填
`localhost`。要簽章就在 `.env` 設 `EEM_SIGNING_PFX` 與 `EEM_SIGNING_PASSWORD`
（見 `agent/signing/README.md`）。

### 8.1 改了 Agent 原始碼之後：「重建 Agent 程式」

產生安裝包只是把**事先建好**的 `EndpointAgent.exe` 重新包一次（`wix build`，約
6 秒），**不會編譯 C#**。這是刻意的：每個安裝包裡的 Agent 執行檔位元組完全相同，
只有設定檔不同，每產一包就重編是白花分鐘級的時間。

代價是「改了 Agent 原始碼卻沒重建」會裝到舊程式，而且**毫無徵兆**。所以：

* 「安裝包」頁會比對原始碼與執行檔的時間戳記，過期時顯示紅字：
  *「Agent 原始碼比已建置的執行檔新：現在產生的安裝包會裝到舊版 Agent。」*
* 旁邊的 **「重建 Agent 程式」** 按鈕會在伺服器上跑一次建置（背景執行，約一到
  兩分鐘，頁面每 5 秒更新狀態）。跑完再產包就是新版。
* `install.ps1` 也會在原始碼較新時自動重建，所以重跑安裝腳本一樣有效。

限制與界線：

| 項目 | 說明 |
| --- | --- |
| 誰能按 | **僅最高管理員**。這會換掉派送到每一台端點的執行檔，比產生單一安裝包更敏感，所以不隨「安裝包」功能授權一起下放 |
| 需要什麼 | 伺服器上要有 `C:\EEM\agent\src`（`install.ps1` 會複製）與 .NET SDK |
| 這不是遠端執行 | 命令列固定寫在程式碼裡，不接受請求傳來的任何參數，跑的是伺服器自己磁碟上、只有系統管理員寫得進去的原始碼。等同於 `agent\build.ps1`，只是換一個入口（CLAUDE.md §16） |
| 稽核 | 每次啟動重建都寫 `REBUILD_AGENT` |
| 沒有原始碼時 | 按鈕不出現，回傳 409 並說明要改在有原始碼的機器跑 `agent\build.ps1` |

---

## 9. 備份

| 對象 | 內容 |
| --- | --- |
| `C:\EEM\server\.env` | `EEM_SECRET_KEY`（外洩＝可偽造登入權杖） |
| `C:\EEM\server\instance\` | `eem.db`、`recording.key`、`tls\`、錄影、截圖、安裝包 |

```powershell
Stop-ScheduledTask -TaskName EEMManagementServer
Compress-Archive -Path C:\EEM\server\.env, C:\EEM\server\instance -DestinationPath "D:\eem-backup-$(Get-Date -Format yyyy-MM-dd).zip"
Start-ScheduledTask -TaskName EEMManagementServer
```

⚠️ **遺失 `recording.key` ＝ 既有錄影／截圖永遠無法解密。**

---

## 10. 疑難排解

| 症狀 | 處理 |
| --- | --- |
| 瀏覽器打不開 | 用安裝時印出的 `https://<IP>`（**一定要 https**）。跨機器連不到就看防火牆與網路；VM 用 NAT 的話宿主機連不到，要改橋接 |
| 「連線不安全」警告 | 自簽憑證的正常行為，點「進階 → 繼續前往」；根治見 §7 |
| 服務沒起來 | `Get-Content C:\EEM\server\instance\server.log -Tail 30`。常見：443 被其他程式佔用（改 `-Port`） |
| 443 被佔用 | `Get-NetTCPConnection -LocalPort 443 -State Listen`（IIS、Skype 之類）→ 停掉它或改用 `-Port 8443` 重裝 |
| 「安裝包」頁說找不到 WiX | 安裝時的 WiX 步驟沒過（沒有 winget、或裝 .NET SDK 失敗）。確認 `dotnet --list-sdks` 有輸出，再重跑 `install.ps1`。訊息括號裡若是 `wix` 而不是完整路徑，代表 `.env` 沒有 `EEM_WIX_COMMAND`——就是這個情況 |
| MSI 建置失敗 `WIX0144` | WiX 擴充沒進 SYSTEM 設定檔，重跑 `install.ps1` 會修好 |
| 端點顯示離線 | Agent 的 Server URL 不對，或憑證不受信任（見 §7） |
| 一般管理員看不到某些頁 | **預設行為**：最高管理員到「管理員 → 功能授權」逐項開放 |

---

## 11. 上線前檢查

- [ ] `C:\EEM\server\.venv\Scripts\python.exe -m flask --app wsgi check-config` 無 PROBLEM
- [ ] 憑證不是自簽的，或內部 CA 憑證已派送到所有端點
- [ ] 正式資料量改用 PostgreSQL（`EEM_DATABASE_URI`）
- [ ] `.env` 與 `instance\` 已排定備份
- [ ] 安裝時印出的管理員密碼已保存或已在主控台改過
- [ ] 螢幕監控／錄影的**書面同意**已就緒（見 `CLAUDE.md` §2、§14.4）
