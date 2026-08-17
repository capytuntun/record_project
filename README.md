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
**以系統管理員身分**開啟 PowerShell，執行：

```powershell
powershell -ExecutionPolicy Bypass -File server\install.ps1
```

跑完就是可以登入的系統，最後會印出網址與管理員密碼，密碼只會顯示一次


##  需求

| 項目 | 說明 |
| --- | --- |
| 作業系統 | Windows 10／11／Server 2019 以上 |
| 權限 | 系統管理員（UAC） |
| Python | **3.11 以上**；沒有的話腳本會試著用 winget 裝 |
| .NET SDK | 只有「要在主控台建 MSI」才需要（WiX 與 Agent 執行檔都靠它）；沒有的話腳本會用 winget 裝 |
| FFmpeg | 只有「螢幕錄影」需要，放在 `server\tools\ffmpeg\ffmpeg.exe` |

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