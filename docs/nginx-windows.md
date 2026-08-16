# Windows + nginx 反向代理教學

> **多數 Windows 部署不需要這一篇。** `server/install.ps1` 預設讓程式**自己終結
> TLS**（`0.0.0.0:443`），裝完就能用 `https://` 連，不需要任何代理。本文是給
> 「一定要在前面擺 nginx」的情境：共用 443、公司規定統一走反向代理、需要
> nginx 的存取控制或記錄格式。
>
> ⚠️ **驗證狀態**：本文的設定是依 `install.ps1` 實際產生的檔案路徑、ACL 與
> `wsgi.py` 的繫結邏輯寫成，但**尚未在實機跑過一遍**。第一次照做時請按 §9
> 逐項驗證，特別是 WebSocket（即時畫面）與稽核來源 IP 兩項。

```
瀏覽器 / Agent ──HTTPS(443)──▶ nginx ──http(127.0.0.1:5000)──▶ EEM (wsgi.py)
                （TLS 在這裡終結）          （只在本機回環，不對外）
```

---

## 0. 你真的需要嗎

| 你想要的 | 需要 nginx 嗎 |
| --- | --- |
| 只是要 HTTPS | **不需要** —— 內建就有（`install.ps1` 已設好） |
| 同一台還要跑別的網站，共用 443 | 需要 |
| 公司規定所有對外服務走統一反向代理 | 需要 |
| 想要 nginx 的來源限制、rate limit、統一存取記錄 | 需要 |
| 想要「更快」 | **不要** —— Windows 版 nginx 反而是瓶頸，見 §10 |

---

## 1. 改動前後的差異

| 項目 | 改之前（預設） | 改之後（本文） |
| --- | --- | --- |
| 誰聽 443 | `python.exe`（wsgi.py） | `nginx.exe` |
| TLS 終結在 | EEM 自己 | nginx |
| EEM 聽哪裡 | `0.0.0.0:443`（HTTPS） | `127.0.0.1:5000`（**純 HTTP**，只在回環） |
| 憑證由誰讀 | EEM（SYSTEM） | nginx（**必須也是 SYSTEM**，見 §5） |
| 稽核來源 IP 來自 | TCP 對端 | `X-Forwarded-For`（要開 `EEM_TRUST_PROXY_HEADERS`） |

---

## 2. 事前確認

在**系統管理員** PowerShell 執行：

```powershell
Test-Path C:\EEM\server\.env                       # True
(Get-ScheduledTask EEMManagementServer).State      # Running
curl.exe -k https://127.0.0.1/api/health           # {"status":"ok"}
```

三項都過再往下。（`curl.exe` 是 Windows 10 以上內建的，`-k` 是略過自簽憑證驗證。
注意要寫 `curl.exe`，直接打 `curl` 會被 PowerShell 導到 `Invoke-WebRequest`。）

---

## 3. 安裝 nginx

1. 到 <https://nginx.org/en/download.html>，下載 **Stable version** 底下的
   `nginx/Windows-x.xx.x`（zip 檔）。
2. 解壓到 **`C:\nginx`**。

   > 路徑**不要有空白、中文或非 ASCII 字元** —— Windows 版 nginx 對這類路徑處理
   > 不佳，會出現很難查的啟動失敗。
3. 確認：

```powershell
C:\nginx\nginx.exe -v          # nginx version: nginx/1.28.x
```

---

## 4. 讓 EEM 退到 `127.0.0.1:5000`

**這一步一定要先做**，否則 nginx 起不來（443 還被 EEM 佔著）。

用系統管理員權限編輯 `C:\EEM\server\.env`（這個檔的 ACL 只給 SYSTEM 與
Administrators，一般權限開不起來）：

```ini
# 改成這樣（原本是 0.0.0.0 / 443）
EEM_BIND_HOST=127.0.0.1
EEM_BIND_PORT=5000

# 這兩行「整行註解掉」——沒有憑證，wsgi.py 就以純 HTTP 啟動
#EEM_TLS_CERT=C:\EEM\server\instance\tls\cert.pem
#EEM_TLS_KEY=C:\EEM\server\instance\tls\key.pem

# 前面確定是自己這台 nginx 才設 1
EEM_TRUST_PROXY_HEADERS=1
```

三個設定各自的道理：

* **繫結回環**：`127.0.0.1` 讓後端只能從本機連 —— 外面繞過 nginx 直接打 5000 是
  連不到的。這比「開 5000 但用防火牆擋」更可靠。
* **註解掉憑證**：`wsgi.py` 的 `_bind_settings()` 是看 `EEM_TLS_CERT` /
  `EEM_TLS_KEY` **有沒有值**來決定要不要包 TLS。兩者留空 → 純 HTTP。
  留著舊值會變成 nginx 與 EEM 各做一次 TLS，連不起來。
* **信任代理標頭**：`app/request_context.py` 的 `client_ip()` 預設**故意忽略**
  `X-Forwarded-For`（不然任何人都能偽造稽核紀錄裡的來源 IP）。前面真的擺了會
  覆寫該標頭的 nginx，才把它打開。

改完重啟並確認後端改用 HTTP：

```powershell
Restart-ScheduledTask -TaskName EEMManagementServer
Start-Sleep 3
curl.exe http://127.0.0.1:5000/api/health      # 注意是 http，不是 https
```

---

## 5. 憑證：直接沿用現成的

`install.ps1` 產生的就是 **PEM**（用 Python `cryptography` 產生／從 PFX 轉出），
nginx 可以直接吃，不用再轉檔：

```
C:\EEM\server\instance\tls\cert.pem     # 憑證（用 -CertPath 匯入公司憑證時，含中繼鏈）
C:\EEM\server\instance\tls\key.pem      # 私鑰
```

> ⚠️ **私鑰的 ACL 只給 SYSTEM 與 Administrators 讀**（`install.ps1` 用 `icacls`
> 鎖的）。所以 **nginx 必須以 SYSTEM 身分執行** —— §7 的排程工作就是這樣設的。
> 若你改用一般帳號跑 nginx，worker 會讀不到私鑰而啟動失敗
> （`error.log` 出現 `SSL_CTX_use_PrivateKey_file ... Permission denied`）。

換成公司憑證的兩種做法：

* 重跑 `install.ps1 -CertPath C:\certs\eem.pfx -CertPassword '密碼'`，PFX 會被轉成
  上面那兩個 PEM，nginx 不用改設定。
* 或自己另外放一份 PEM 給 nginx 用，記得同樣把私鑰 ACL 鎖起來：

```powershell
icacls C:\nginx\conf\key.pem /inheritance:r /grant "SYSTEM:(R)" "Administrators:(R)"
```

自簽憑證的老問題不會因為換成 nginx 而消失：**端點的 Agent 走 `wss://` 連同一個
443，憑證不受信任就連不上**。內網請把這張憑證匯入端點的「受信任的根憑證授權」
（可用 GPO 派送），或改用公司內部 CA 簽發。

---

## 6. nginx.conf

覆蓋 `C:\nginx\conf\nginx.conf`。三件 Windows 特有的注意事項：

1. 路徑一律用 **正斜線** `/`（反斜線在 nginx 設定裡是跳脫字元）。
2. 檔案存成 **UTF-8 無 BOM**。存成有 BOM 的話 nginx 會報
   `unknown directive "﻿worker_processes"`（前面那個看不見的字元就是 BOM）。
   VS Code 右下角選「UTF-8」而不是「UTF-8 with BOM」；PowerShell 請用
   `Set-Content -Encoding utf8` 之外的方式（PS 5.1 的 `utf8` 會帶 BOM）。
3. 第一行的 `daemon off;` 不要拿掉 —— §7 的排程工作需要 nginx 留在前景。

```nginx
# 排程工作要看得到行程活著，所以不要讓 nginx 自己跑到背景。
daemon off;

worker_processes  1;          # Windows 版多 worker 不會好好共享 listen socket
error_log  logs/error.log warn;

events {
    worker_connections  1024;
}

http {
    include       mime.types;
    default_type  application/octet-stream;
    sendfile      off;        # Windows 版不支援 sendfile
    keepalive_timeout  65;
    access_log  logs/access.log;

    # WebSocket 升級用的對應
    map $http_upgrade $connection_upgrade {
        default upgrade;
        ''      close;
    }

    # 80 → 443
    server {
        listen      80;
        server_name _;
        return 301 https://$host$request_uri;
    }

    server {
        listen      443 ssl;
        http2       on;                    # nginx 1.25.1 以前請改寫成 listen 443 ssl http2;
        server_name _;

        ssl_certificate     C:/EEM/server/instance/tls/cert.pem;
        ssl_certificate_key C:/EEM/server/instance/tls/key.pem;
        ssl_protocols       TLSv1.2 TLSv1.3;

        # 程式在代理後方看不到 TLS，HSTS 由 nginx 送
        add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;

        # 截圖上傳最大約 25MB，留點餘裕
        client_max_body_size 32m;

        location / {
            proxy_pass http://127.0.0.1:5000;
            proxy_http_version 1.1;

            # WebSocket：即時畫面、螢幕牆、Agent 回連都靠這兩行
            #   /api/agent/screen/ws                  （Agent 端）
            #   /api/endpoints/<id>/screen/ws         （主控台端）
            proxy_set_header Upgrade    $http_upgrade;
            proxy_set_header Connection $connection_upgrade;

            proxy_set_header Host              $host;
            proxy_set_header X-Real-IP         $remote_addr;
            proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;

            # 螢幕串流是長連線，別中途被切
            proxy_read_timeout 3600s;
            proxy_send_timeout 3600s;
        }

        # MSI 安裝包有數十 MB（實測 45MB），關掉緩衝直接串流給瀏覽器，
        # 不要整包先落到 nginx 的暫存檔再送。
        location /api/packages/ {
            proxy_pass http://127.0.0.1:5000;
            proxy_http_version 1.1;
            proxy_set_header Host              $host;
            proxy_set_header X-Real-IP         $remote_addr;
            proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
            proxy_buffering    off;
            proxy_read_timeout 600s;
        }
    }
}
```

檢查語法：

```powershell
C:\nginx\nginx.exe -p C:\nginx -t      # ... syntax is ok / test is successful
```

---

## 7. 註冊成開機自動啟動

和管理伺服器同一套做法：**排程工作**，不是 Windows 服務。理由一樣 ——
`nginx.exe` 不會回應服務控制管理員，`sc create` 註冊會啟動失敗，而排程工作用
「開機觸發 + SYSTEM + 失敗自動重啟」達成等價效果，且不必額外裝 NSSM。

系統管理員 PowerShell：

```powershell
$nginx     = "C:\nginx"
$action    = New-ScheduledTaskAction -Execute "$nginx\nginx.exe" -Argument "-p `"$nginx`"" -WorkingDirectory $nginx
$trigger   = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
                -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 `
                -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "EEMNginx" -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Description "螢幕測錄系統 反向代理" -Force
Start-ScheduledTask -TaskName "EEMNginx"
```

> 少了設定檔第一行的 `daemon off;`，`nginx.exe` 會立刻返回，排程工作判定「已結束」，
> 再配上「失敗自動重啟」就會變成無限反覆拉起。

日常操作：

| 動作 | 指令 |
| --- | --- |
| 重新載入設定（不斷線） | `C:\nginx\nginx.exe -p C:\nginx -s reload` |
| 停止 | `Stop-ScheduledTask -TaskName EEMNginx` |
| 啟動 | `Start-ScheduledTask -TaskName EEMNginx` |
| 看有沒有在跑 | `Get-Process nginx` |
| 看錯誤 | `Get-Content C:\nginx\logs\error.log -Tail 30` |

---

## 8. 防火牆

`install.ps1` 已經放行 TCP 443（規則名 **螢幕測錄系統**），nginx 接手同一個埠，
**不用改**。只有要用 80 轉址才多開一條：

```powershell
New-NetFirewallRule -DisplayName "螢幕測錄系統 HTTP 轉址" -Direction Inbound `
    -Protocol TCP -LocalPort 80 -Action Allow -Profile Any
```

**不要**對外開放 5000 —— 後端只監聽回環，開了也沒用，只會讓人誤會。

確認 443 現在是 nginx 在聽（不是 python）：

```powershell
# 別把變數取名 $pid —— 那是 PowerShell 的自動變數（目前行程的 PID），唯讀。
$owner = (Get-NetTCPConnection -LocalPort 443 -State Listen).OwningProcess | Select-Object -First 1
Get-Process -Id $owner | Select-Object Id, ProcessName
```

---

## 9. 驗證

照順序做完，每一項都對才算完成：

| # | 驗什麼 | 怎麼驗 | 期望 |
| --- | --- | --- | --- |
| 1 | 後端活著 | `curl.exe http://127.0.0.1:5000/api/health` | `{"status":"ok"}` |
| 2 | 代理通了 | `curl.exe -k https://127.0.0.1/api/health` | 同上 |
| 3 | 從別台連得到 | 另一台開 `https://<伺服器IP>` | 登入頁 |
| 4 | **WebSocket** | 登入 → 隨便一台端點「檢視畫面」 | 看得到即時畫面，不是卡在「等待畫面」 |
| 5 | **Agent 方向** | 端點清單 | 端點仍是「線上」（心跳與 `wss` 都穿過了） |
| 6 | **稽核來源 IP** | 從別台機器登入 → 看「稽核紀錄」 | 來源 IP 是那台的位址，**不是** `127.0.0.1` |
| 7 | 大檔下載 | 產一個安裝包並下載 | MSI 完整（數十 MB），不是 0 位元組或中斷 |

第 6 項失敗（IP 都是 `127.0.0.1`）代表 `EEM_TRUST_PROXY_HEADERS` 沒生效 ——
這是最容易漏掉的一項，而且**不會有任何錯誤訊息**，只是稽核紀錄從此失去意義。

---

## 10. Windows 版 nginx 的限制（先知道再決定）

nginx 官方把 Windows 版定位成**移植版本、beta 品質**，不是 Linux 版的等價替代：

* 連線處理只用 `select()` / `poll()`，沒有 Linux 的 `epoll`。連線數一多，效能掉得
  比 Linux 明顯。
* 多個 worker 在 Windows 上無法好好共享 listen socket，所以 §6 設
  `worker_processes 1`（單一 worker 也就吃單核）。
* 不支援 `sendfile`、Unix domain socket。

對這套系統的實際影響：**螢幕牆是長連線大戶** —— 每台被檢視的端點一條 Agent WS，
每個檢視者再一條 WS，全部都要佔著 nginx 的連線額度好幾分鐘。小規模內網
（數十台端點）夠用；規模再大，把管理伺服器搬到 Linux
（[`deployment.md`](deployment.md) + [`nginx-ubuntu.md`](nginx-ubuntu.md)）
會比在 Windows 上調 nginx 划算得多。

**加上 nginx 不會讓這套系統變快。** 值得做的理由是共用埠、統一代理、存取控制 ——
不是效能。

---

## 11. 重跑 `install.ps1` 會發生什麼

升級時你會重跑安裝腳本，它與本文設定的互動：

| 項目 | 行為 |
| --- | --- |
| `.env` | **不會被覆蓋**（「沿用既有 .env」），你在 §4 的改動留著 |
| TLS 憑證 | 沿用既有的，不會重新產生 |
| 排程工作 `EEMManagementServer` | 重新註冊並啟動（正常） |
| 排程工作 `EEMNginx` | 腳本不知道它的存在，**不會動它** |
| 最後的健康檢查 | 它打的是 `https://127.0.0.1:443/api/health` —— 現在那是 nginx |

⚠️ 所以**重跑 `install.ps1` 之前要確認 nginx 正在跑**，否則腳本會在最後的健康檢查
失敗並中止（`啟動失敗。修正後可直接重跑本腳本。`）—— 但那時程式碼其實已經更新完了。

---

## 12. 還原成內建 TLS

不想用 nginx 了：

```powershell
Stop-ScheduledTask   -TaskName EEMNginx
Unregister-ScheduledTask -TaskName EEMNginx -Confirm:$false
```

再把 `.env` 改回去：

```ini
EEM_BIND_HOST=0.0.0.0
EEM_BIND_PORT=443
EEM_TLS_CERT=C:\EEM\server\instance\tls\cert.pem
EEM_TLS_KEY=C:\EEM\server\instance\tls\key.pem
EEM_TRUST_PROXY_HEADERS=0
```

```powershell
Restart-ScheduledTask -TaskName EEMManagementServer
curl.exe -k https://127.0.0.1/api/health
```

---

## 13. 疑難排解

| 症狀 | 原因與處理 |
| --- | --- |
| `bind() to 0.0.0.0:443 failed (10048)` | 443 還被 EEM 佔著 —— §4 沒做或沒重啟排程工作 |
| `unknown directive "﻿worker_processes"` | `nginx.conf` 存成 **UTF-8 with BOM**，改存無 BOM |
| `SSL_CTX_use_PrivateKey_file ... Permission denied` | nginx 不是以 SYSTEM 執行，讀不到 `key.pem`（§5 的 ACL） |
| nginx 一直被反覆重啟 | 設定檔少了 `daemon off;`（§7） |
| `502 Bad Gateway` | 後端沒起來：`Get-Content C:\EEM\server\instance\server.log -Tail 30` |
| 即時畫面卡在「等待畫面」 | `map` / `Upgrade` / `Connection` 三者沒設對，改完 `nginx -s reload` |
| `413 Request Entity Too Large` | 調高 `client_max_body_size` |
| 稽核來源 IP 都是 `127.0.0.1` | `.env` 沒設 `EEM_TRUST_PROXY_HEADERS=1`，或改了沒重啟 |
| MSI 下載中斷 | `location /api/packages/` 的 `proxy_buffering off;` 沒生效，確認 §6 那段有加 |
| 端點全部離線 | Agent 走 `wss://` 到同一個 443，憑證換過就要重新讓端點信任（§5） |
| 瀏覽器一直憑證警告 | 自簽憑證未受信任 → 匯入 CA，或改用公司內部 CA |

---

## 14. 相關文件

* [`deployment-windows.md`](deployment-windows.md) —— Windows 安裝主文（預設不用 nginx）
* [`nginx-ubuntu.md`](nginx-ubuntu.md) —— Linux 版的同一件事（設定內容幾乎一致）
* [`deployment.md`](deployment.md) —— Linux 安裝主文
