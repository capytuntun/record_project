# 螢幕側錄系統
這是一套基於「主從式架構」設計的終端操作側錄與資安稽核系統，主要透過中控端與端點代理程式的協同運作，來實現全面的使用者行為監控與管理。

這套側錄系統具備高度彈性的稽核策略與完善的端點防護機制，能有效平衡安全需求與系統資源。以下針對其關鍵功能特色進行詳細說明：


- 彈性的側錄策略（全錄 vs. 差異式）
為因應不同層級的資安需求並最佳化資源配置，系統提供兩種主要的側錄模式：
    - 全錄（連續側錄）： 類似監視器不間斷地錄製所有畫面，適用於高敏感度設備、跳板機或特權帳號操作，確保稽核紀錄零死角。
    - 差異式側錄： 系統僅在螢幕畫面發生變動、或使用者有具體操作（如滑鼠點擊、鍵盤敲擊、切換視窗）時才進行紀錄。此模式能自動濾除閒置時的靜止畫面，大幅節省儲存空間與網路頻寬。
- 自動化的儲存與生命週期管理（支援 FTP / SMB）
    - 異地與集中儲存： 系統支援標準的 FTP 與 SMB（網路共用/NAS） 通訊協定。這意味著企業可以靈活地將龐大的側錄影音檔與日誌，拋轉至現有的檔案伺服器或 NAS 設備中集中存放，無須受限於中控伺服器本身的硬碟容量。
    - 設定保存時間（Retention Policy）： 管理者可依據法規遵循（如 ISO 27001）或企業內規，自訂資料的保存期限（例如 90 天或 180 天）。系統會自動執行資料生命週期管理，定期清除逾期的歷史紀錄，實現儲存空間的循環利用。

為防止內部員工蓄意規避稽核或惡意程式破壞，部署於終端設備上的 Agent 具備極高的防篡改（Tamper Protection）層級。使用者無法輕易透過工作管理員強制結束處理程序；若要停止服務或移除端點代理程式，必須輸入由中控端配發的專屬密碼，確保側錄政策的強制性與系統的完整性。

## 安裝方式

**圖形安裝精靈（下一步、下一步）** — 把 `EEMServerSetup-<版本>.exe` 複製到目標電腦，
按兩下、依畫面操作即可；最後一頁顯示網址與只顯示一次的管理員密碼。這個 `.exe` 用
`server\packaging\Build-ServerSetup.ps1` 打包產生（詳見
[`docs/deployment-windows.md`](docs/deployment-windows.md) §1A）。

**或用 PowerShell 一行** — **以系統管理員身分**開啟 PowerShell，執行：

```powershell
powershell -ExecutionPolicy Bypass -File server\install.ps1
```

兩種方式結果相同（背後都是 `install.ps1`）。跑完就是可以登入的系統，最後會印出
網址與管理員密碼，密碼只會顯示一次。

## 移除伺服器
**以系統管理員身分**開啟 PowerShell，在專案根目錄執行：

```powershell
powershell -ExecutionPolicy Bypass -File server\uninstall.ps1
```

畫面會列出將刪除的內容，輸入 `DELETE`（大寫）才會開始刪除，輸入其他內容則取消、不做任何變更。

| 寫法 | 效果 |
| --- | --- |
| `server\uninstall.ps1` | 完全刪除（要輸入 `DELETE` 確認） |
| `server\uninstall.ps1 -KeepData` | 只移除服務與防火牆規則，`C:\EEM` 保留；之後重跑 `install.ps1` 即可原樣復原 |
| `server\uninstall.ps1 -Force` | 完全刪除，不詢問（無人值守用） |
| `server\uninstall.ps1 -InstallDir D:\EEM` | 安裝時改過位置的話，用這個指定（必須是完整路徑） |

完全刪除會依序：
1. 停止並移除排程工作 `EEMManagementServer`
2. 結束相關行程（伺服器的 python、錄影中的 ffmpeg、建置安裝包中的 wix）
3. 移除防火牆規則「螢幕測錄系統」
4. 刪除服務帳號（SYSTEM）設定檔裡的 WiX 擴充
5. 刪除整個 `C:\EEM`：資料庫、所有錄影與截圖、畫面加密金鑰、TLS 憑證與私鑰、`.env`、FFmpeg、WiX

**不會**刪除的：
- Python、.NET SDK：共用元件，要移除請到「設定 → 應用程式」
- `.env` 指到 `C:\EEM` 以外的資料（例如 `EEM_RECORDING_DIR`、外部資料庫）：結束時會列出位置
- 已拋轉到 FTP／SMB 的錄影
- 端點上的 Agent：請在各端點用 `msiexec /x` 解除安裝（見下方〈端點安裝包〉）。解除密碼存在安裝包裡，伺服器刪掉之後端點仍可解除安裝

> ⚠️ 刪除後**無法復原**。要保留錄影，先備份 `C:\EEM\server\instance\`（一定要含 `recording.key`，少了它錄影無法解密）。

有檔案刪不掉時（通常是還有程式開著），腳本會列出剩下的檔案並以失敗結束。這時重新開機，再執行一次 `server\uninstall.ps1` 即可。

為了防止誤刪，遇到以下情況會拒絕刪除：磁碟根目錄、相對路徑、原始碼資料夾（有 `.git`），或底下沒有 `server\` 的資料夾。


##  需求

| 項目 | 說明 |
| --- | --- |
| 作業系統 | Windows 10／11／Server 2019 以上 |
| 權限 | 系統管理員（UAC） |
| Python | **3.11 以上**；沒有的話腳本會試著用 winget 裝 |
| .NET SDK | 只有「要在主控台建 MSI」才需要（WiX 與 Agent 執行檔都靠它）；沒有的話腳本會用 winget 裝 |
| FFmpeg | 只有「螢幕錄影」需要；repo 已內附 `server\tools\ffmpeg\ffmpeg.zip`（來源與授權見同目錄 `SOURCE.md`），腳本會解壓到 `C:\EEM\tools\ffmpeg\ffmpeg.exe`，不必下載。缺檔時才自動從 gyan.dev 下載（核對 SHA-256），或用 `-FfmpegPath <ffmpeg.exe>` 指定 |

## 服務控制

```powershell
Get-ScheduledTask -TaskName EEMManagementServer          # 狀態
Restart-ScheduledTask -TaskName EEMManagementServer      # 重啟（改完 .env 要重啟）
Stop-ScheduledTask -TaskName EEMManagementServer         # 停止
Get-Content C:\EEM\server\instance\server.log -Tail 30   # 記錄
```

## 端點安裝包

主控台「安裝包」頁 → 填標籤、伺服器網址、有效期間、使用次數、管理密碼 → 建立，
伺服器會在約 10 秒內建好 MSI 並提供下載。端點安裝：
- 安裝
```powershell
msiexec /i <下載的>.msi /qn
```
- 解除安裝
安裝包沒設 IT 密碼：
```
msiexec /x "{產品碼}" /qn
```
安裝包有設 IT 密碼（產生安裝包時填的管理員密碼）：
```
msiexec /x "{產品碼}" UNINSTALLPWD=<IT密碼> /qn
```
要看進度／記 log 的話：
```
msiexec /x "{產品碼}" UNINSTALLPWD=<IT密碼> /qb /l*v C:\Windows\Temp\eem-uninstall.log
```
從「設定 → 應用程式」移除時會跳出密碼框，輸入 IT 密碼即可。

驗證密碼保護有生效時，**一定要連錯誤密碼一起測**：`UNINSTALLPWD=隨便打` 必須失敗、
程式必須還在，log 裡要有 `uninstall blocked -- BlockWrongPassword`；只測正確密碼看不出
保護是否被略過（曾有一版因設定檔的雜湊被轉義而對任何密碼放行）。對應的單元測試在
`agent\src\EndpointAgent.CustomActions.Tests`（`dotnet test` 執行）。