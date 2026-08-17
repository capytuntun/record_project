# 螢幕測錄系統 — Linux 安裝手冊

本手冊說明如何把「螢幕測錄系統」的 **Web 管理主控台 + 後端伺服器**
（`server/` 目錄）裝到一台 Linux 上。

> **要裝在 Windows 上？** 見 [`deployment-windows.md`](deployment-windows.md)
> （一樣是一鍵安裝）。差別只有一個但很關鍵：**主控台的「安裝包」功能只有
> Windows 版能用**，因為 WiX 只支援 Windows（見 §8.1）。

> 一句話架構：`server/` 是一支 Flask 應用，**同時**提供 REST API、WebSocket
> 即時畫面、以及網頁主控台（`/` 直接就是登入頁）。沒有另外要部署的前端。
> 端點的 Windows Agent 另行安裝，透過此伺服器的公開網址回連。

---

## 1. 一行指令安裝

把程式碼放上伺服器後，**只要這一行**：

```bash
sudo bash server/install.sh
```

正式環境（有網域、要正式憑證）加兩個參數：

```bash
sudo bash server/install.sh --domain management.example.com --email you@example.com
```

> ⚠️ **`management.example.com` 只是文件範例**。`example.com` 是 RFC 2606 保留給
> 文件用的網域，**永遠不會指向你的伺服器**——照抄去瀏覽器打當然是空的。
>
> - 有自己的網域：換成它，並確認該網域的 **DNS A 記錄指向這台伺服器的 IP**。
> - 沒有網域：**不要加 `--domain`**，安裝完直接用腳本印出來的
>   `https://<伺服器 IP>` 連（憑證是自簽的，瀏覽器會先跳警告，選「繼續前往」）。
> - 只是想在本機看看：裝在同一台機器上，用 `https://localhost` 連。

跑完就是可以登入的系統：系統套件、虛擬環境、隨機密鑰、資料庫、第一個最高
管理員、systemd 服務、nginx + HTTPS 全部就緒，最後印出主控台網址與管理員密碼。

```
══════════════════════════════════════════════════════════
 安裝完成
══════════════════════════════════════════════════════════

  主控台網址   https://management.example.com
  管理員帳號   admin
  管理員密碼   7JkX3uPm20sAVQ0uAxTw   ← 只顯示這一次，請立刻保存
```

> **密碼只印這一次**，沒有存成檔案。忘了就用 `sudo bash install.sh --uninstall --purge`
> 重來，或用另一個最高管理員帳號到主控台重設。

### 把程式碼送上伺服器

三種擇一，`install.sh` 都吃得下：

```bash
# A. 從你的電腦複製過去（Windows PowerShell 也有 scp）
scp -r ./record you@server:/root/
ssh you@server 'sudo bash /root/record/server/install.sh'

# B. 放在內部檔案伺服器，遠端一行搞定
curl -fsSL https://files.example.com/install.sh | sudo bash -s -- \
    --source https://files.example.com/eem.tar.gz --domain management.example.com

# C. 從內部 Git
sudo bash install.sh --source https://git.example.com/eem.git
```

---

## 2. 需求

| 項目 | 說明 |
| --- | --- |
| 作業系統 | **Debian／Ubuntu 系**（腳本用 `apt`）。已驗證：Ubuntu 26.04 LTS。其他發行版見 §9 手動安裝 |
| 權限 | root（`sudo`） |
| Python | **3.11 以上**；腳本會自動挑最新的，必要時補裝 `python3.X-venv` |
| 網路 | 安裝時要能連 apt 與 PyPI |
| 資料庫 | 預設 SQLite（免安裝）。正式量體建議 PostgreSQL（§6） |
| FFmpeg | 只有「螢幕錄影」需要；腳本預設幫你裝，`--no-ffmpeg` 可略過 |

安裝完的樣子：

```
/opt/eem/
├── server/            ← 程式碼（root 所有，服務帳號唯讀）
│   ├── .venv/         ← 虛擬環境
│   ├── .env           ← 密鑰與設定（0640 root:eem）
│   └── instance/      ← 資料庫、加密金鑰、錄影、截圖、安裝包（唯一可寫處）
├── agent/             ← Agent 產物（產生 MSI 安裝包用）
└── docs/              ← 本手冊
```

程式以服務帳號 `eem` 執行，**只監聽 `127.0.0.1:5000`**；對外一律經過 nginx（TLS 在
那裡終結）。

---

## 3. 安裝程式做了什麼

| 步驟 | 內容 |
| --- | --- |
| 1. 系統套件 | `ffmpeg`、`nginx`、`rsync`、`openssl`、`curl` 等 |
| 2. Python | 挑 3.11+ 的直譯器，補裝對應的 `-venv`，建立 `.venv` 並裝 `requirements.txt` |
| 3. 佈署 | 用 `rsync` 放到 `/opt/eem/server`，一併複製 `agent/` 產物與 `docs/` |
| 4. `.env` | 產生隨機 `EEM_SECRET_KEY`（64 bytes），權限 0640，**不含任何預設密碼** |
| 5. 資料庫 | `flask init-db` 建立資料表（`instance/eem.db`） |
| 6. 最高管理員 | `flask bootstrap-super-admin`，密碼隨機產生（20 碼）或由 `EEM_ADMIN_PASSWORD` 指定 |
| 7. 權限 | 程式碼 root 所有、服務帳號唯讀；只有 `instance/` 可寫 |
| 8. 服務 | 寫入 `/etc/systemd/system/eem.service` 並 `enable --now`（開機自動啟動） |
| 9. 驗證 | 等 `/api/health` 回 `{"status":"ok"}`，逾時就印 journal 並中止 |
| 10. nginx | 站台 + 自簽或 Let's Encrypt 憑證、WebSocket 轉發、80→443 導向 |
| 11. 部署前檢查 | 跑 `flask check-config`，把 WARNING／PROBLEM 印出來 |

安全性上值得一提的兩點：

- **安裝視窗不落地**：建立第一個管理員需要的 `EEM_BOOTSTRAP_SECRET` 只存在於安裝
  當下的記憶體，從不寫進 `.env`，所以沒有「事後忘了刪掉」的問題。
- **來源權限不繼承**：從 Windows 共用或 USB 複製過來的檔案常是 777；腳本會一律
  收回群組／其他人的寫入權，避免本機任何人都能改寫以服務身分執行的程式碼。

---

## 4. 選項

```
--domain <網域>     對外網域（設定 nginx server_name 與憑證）
--email <信箱>      搭配 --domain 用 Let's Encrypt 申請正式憑證；不給則自簽
--source <來源>     程式碼來源：目錄、.tar.gz 網址、或 .git 網址
--dir <路徑>        安裝位置，預設 /opt/eem
--user <帳號>       服務執行帳號，預設 eem
--admin <帳號>      第一個最高管理員的使用者名稱，預設 admin
--no-proxy          不裝 nginx（只留 127.0.0.1:5000，自己接代理）
--no-ffmpeg         不裝 FFmpeg（不需要螢幕錄影時）
--uninstall         停用並移除服務，保留 /opt/eem 的資料
--uninstall --purge 連同程式、資料庫、錄影、自簽憑證一併刪除
```

環境變數 `EEM_ADMIN_PASSWORD` 可指定管理員密碼（無人值守安裝用；不給就隨機產生）：

```bash
sudo EEM_ADMIN_PASSWORD='你的密碼' bash server/install.sh --domain management.example.com
```

密碼政策：至少 12 碼，且大寫／小寫／數字／符號四類中至少三類。

---

## 5. 安裝後

### 日常操作

```bash
systemctl status eem          # 狀態
systemctl restart eem         # 重啟（改完 .env 要重啟）
journalctl -u eem -f          # 即時記錄
curl -s http://127.0.0.1:5000/api/health     # 後端健康檢查
curl -sk https://<你的網址>/api/health        # 經 nginx 的健康檢查
```

### 驗證安裝

1. 瀏覽器開 `https://<你的網址>` → 用安裝時印出的帳號密碼登入 → 進入主控台。
2. 一般管理員預設看不到多數功能，這是**設計如此**：最高管理員到
   「管理員 → 功能授權」逐項開放。

### 升級

**重跑同一支腳本即可**，資料不動：

```bash
sudo bash server/install.sh          # 或 --source <新版位置>
```

`.env`、`instance/`（資料庫、加密金鑰、錄影、截圖）都會保留，只換程式碼、更新
相依套件、補上新資料表，然後重啟服務。**被 certbot 或人工改過的 nginx 站台設定
也會原樣保留**（腳本偵測到就不覆寫），所以升級不會把正式憑證換回自簽的。

> ⚠️ `init-db` 只**新增缺少的資料表**，不會改既有欄位。若某版有欄位變動，需自行
> 以 SQL `ALTER TABLE` 補上。**升級前先備份**（§10）。

### 解除安裝

```bash
sudo bash /opt/eem/server/install.sh --uninstall           # 留資料
sudo bash /opt/eem/server/install.sh --uninstall --purge   # 全部刪掉
```

---

## 6. 設定（`.env`）

改完任何一項都要 `sudo systemctl restart eem`。

| 變數 | 必填 | 說明 |
| --- | :--: | --- |
| `EEM_SECRET_KEY` | ✅ | 簽發 JWT 的主密鑰，**至少 32 字元**（安裝時自動產生）。外洩＝可偽造登入權杖 |
| `EEM_DATABASE_URI` | — | 預設 `sqlite:///eem.db`。正式改 `postgresql+psycopg://帳:密@主機/資料庫`（需另裝 `psycopg[binary]`） |
| `EEM_TRUST_PROXY_HEADERS` | — | 反向代理後方設 `1`（安裝腳本裝了 nginx 就會設好），稽核來源 IP 才正確。**未經代理不要設 1**，否則 IP 可偽造 |
| `EEM_ACCESS_TOKEN_TTL_MINUTES` | — | 存取權杖有效分鐘，預設 60 |
| `EEM_REFRESH_TOKEN_TTL_DAYS` | — | 更新權杖（＝登入維持天數），預設 30 |
| `EEM_OFFLINE_AFTER_SECONDS` | — | 幾秒沒心跳視為離線，預設 180 |
| `EEM_LOG_LEVEL` | — | `INFO`（預設）／`WARNING`／`DEBUG` |
| `EEM_RECORDING_KEY` | — | 螢幕錄影／截圖的**加密金鑰**。不設也可以：程式會自動產生並保存到 `instance/recording.key`。正式環境建議由密鑰管理器設此值 |
| `EEM_RECORDING_AUTO_KEY` | — | 預設 `1`。設 `0` 可停用自動金鑰（則未設 `EEM_RECORDING_KEY` 就不錄影） |
| `EEM_FFMPEG_PATH` | — | FFmpeg 路徑。**免設**，自動找系統的 `ffmpeg` |
| `EEM_SIGNING_PFX` / `EEM_SIGNING_PASSWORD` | — | MSI 簽章憑證（見 §8） |

完整選項見 `.env.example` 與 `app/config.py`。所有敏感值都只從環境讀取，不寫死在
程式碼裡。

### 換成 PostgreSQL

```bash
sudo /opt/eem/server/.venv/bin/pip install 'psycopg[binary]'
sudo sed -i 's|^EEM_DATABASE_URI=.*|EEM_DATABASE_URI=postgresql+psycopg://eem:密碼@127.0.0.1/eem|' /opt/eem/server/.env
sudo -u eem bash -c 'cd /opt/eem/server && .venv/bin/python -m flask --app wsgi init-db'
sudo systemctl restart eem
```

（換資料庫等於全新空系統，需要重新 bootstrap 管理員；有資料要先自行搬移。）

---

## 7. HTTPS 與反向代理（nginx）

本程式**自己不供應 HTTPS**，也只監聽 `127.0.0.1:5000`。前面一定要有一台反向代理
負責兩件事：

```
瀏覽器 / Agent ──HTTPS(443)──▶ nginx ──http(127.0.0.1:5000)──▶ EEM
                （TLS 在這裡終結）        （只在本機回環，不對外）
```

1. **終結 TLS**（Agent 與瀏覽器都只用 HTTPS 連進來）
2. **轉發 WebSocket 升級標頭**——即時畫面、螢幕牆、Agent 回連全靠它，少了就會
   一直卡在「已連線，等待畫面」

### 7.1 用安裝腳本（預設，什麼都不用做）

`install.sh` 已經把 nginx 裝好設好，站台檔在 `/etc/nginx/sites-available/eem`：

| 安裝時給的參數 | 結果 |
| --- | --- |
| `--domain` + `--email` | Let's Encrypt 正式憑證，自動續期 |
| 只有 `--domain` | 自簽憑證（`/etc/ssl/eem/`），之後可 `sudo certbot --nginx -d <網域>` 換正式的 |
| 都不給 | 自簽憑證，`server_name _`，用 IP 連（SAN 含本機 IP） |
| `--no-proxy` | 完全不碰 nginx，請照 §7.2 自己架 |

之後重跑 `install.sh` **不會覆寫**被 certbot 或人工改過的站台檔。

### 7.2 從 0 開始自己架 nginx

適用：用 `--no-proxy` 安裝、這台機器已經有 nginx 在跑別的站、或想自己掌握每一行
設定。以下在一台**乾淨的 Ubuntu／Debian** 上從零開始，全部複製貼上即可。

**前提**：EEM 已經在跑。先確認回環通：

```bash
curl -s http://127.0.0.1:5000/api/health      # 應回 {"status":"ok"}
```

#### 步驟 1：安裝 nginx

```bash
sudo apt update
sudo apt install -y nginx
```

#### 步驟 2：WebSocket 升級對應表

`Connection` 標頭要依請求動態決定（一般請求給 `close`，升級請求給 `upgrade`），
所以要先定義一張對應表。放在 `conf.d` 是因為**整台 nginx 只能定義一次**，多站台
時共用：

```bash
sudo tee /etc/nginx/conf.d/eem-websocket.conf >/dev/null <<'EOF'
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}
EOF
```

#### 步驟 3：站台設定

把 `management.example.com` 換成你的網域（沒有網域就寫 `_`，代表接受任何主機名）：

```bash
sudo tee /etc/nginx/sites-available/eem >/dev/null <<'EOF'
# EEM 只監聽回環，對外一律經過這裡（TLS 在此終結）。
server {
    listen 80;
    listen [::]:80;
    server_name management.example.com;
    return 301 https://$host$request_uri;      # 一律轉 HTTPS
}

server {
    listen 443 ssl;
    listen [::]:443 ssl;
    http2 on;
    server_name management.example.com;

    ssl_certificate     /etc/ssl/eem/fullchain.pem;   # 步驟 4 產生
    ssl_certificate_key /etc/ssl/eem/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;

    # 程式在代理後方看不到 TLS，HSTS 由 nginx 送
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;

    # 截圖上傳上限約 25MB，留點餘裕
    client_max_body_size 32m;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_http_version 1.1;

        # ★ 即時畫面／螢幕牆／Agent 回連：少了這兩行 WebSocket 就會失敗
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;

        # 轉發原始主機與來源資訊（稽核的來源 IP 靠這個）
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # 螢幕串流是長連線，別中途被切
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;

        # 大型 MSI 直接串流下載，不要先緩衝
        proxy_buffering off;
    }
}
EOF
```

> **nginx 1.25.1 以下**（例如 Ubuntu 22.04 的 1.18）沒有獨立的 `http2 on;` 指令：
> 刪掉那一行，改寫成 `listen 443 ssl http2;`。版本用 `nginx -v` 看。

#### 步驟 4：憑證（三選一）

**A. 有公開網域 → Let's Encrypt（推薦，自動續期）**

certbot 需要站台先能起來，所以先照 **B** 產一張自簽憑證讓 nginx 啟得動（步驟 5），
再執行：

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d management.example.com --agree-tos -m you@example.com --redirect
```

certbot 會自己改好那兩行並設定自動續期（`sudo certbot renew --dry-run` 可測試）。

**B. 內網／沒有公開網域 → 自簽憑證**

```bash
sudo mkdir -p /etc/ssl/eem
sudo openssl req -x509 -nodes -days 825 -newkey rsa:2048 \
  -keyout /etc/ssl/eem/privkey.pem \
  -out    /etc/ssl/eem/fullchain.pem \
  -subj   "/CN=management.example.com" \
  -addext "subjectAltName=DNS:management.example.com,IP:10.0.0.10"
sudo chmod 600 /etc/ssl/eem/privkey.pem
```

`subjectAltName` **一定要有**（新版瀏覽器不看 CN）；用 IP 連就把那台機器的 IP 也
寫進去。

**C. 公司內部 CA** —— 把簽好的憑證鏈放成 `/etc/ssl/eem/fullchain.pem`、私鑰放成
`/etc/ssl/eem/privkey.pem`（私鑰 `chmod 600`）即可。這是內網部署**最推薦**的做法：
端點本來就信任內部 CA，Agent 不必額外調整。

#### 步驟 5：啟用站台

```bash
sudo ln -sfn /etc/nginx/sites-available/eem /etc/nginx/sites-enabled/eem
sudo rm -f /etc/nginx/sites-enabled/default    # 只有在本站是 catch-all（server_name _）時才需要
sudo nginx -t                                  # 檢查設定語法
sudo systemctl enable --now nginx
sudo systemctl reload nginx
```

#### 步驟 6：防火牆

```bash
sudo ufw allow 'Nginx Full'     # 開放 80 + 443
sudo ufw allow OpenSSH          # ★ 先確認 SSH 有放行，再啟用防火牆
sudo ufw enable
```

**不要**對外開放 `5000`——EEM 只監聽回環，nginx 從內部連它。

#### 步驟 7：讓 EEM 信任代理標頭

沒設這一項，稽核紀錄裡的來源 IP 會全部變成 nginx 的位址：

```bash
sudo sed -i 's/^EEM_TRUST_PROXY_HEADERS=.*/EEM_TRUST_PROXY_HEADERS=1/' /opt/eem/server/.env
grep EEM_TRUST_PROXY_HEADERS /opt/eem/server/.env      # 確認是 1
sudo systemctl restart eem
```

> 只有在**確定前面就是自己這台 nginx**（會覆寫 `X-Forwarded-For`）時才設 1；
> 直接對外時設 1 會讓來源 IP 可被偽造。

#### 步驟 8：驗證

```bash
# 1) HTTPS 通不通（自簽憑證用 -k 略過驗證）
curl -sk https://management.example.com/api/health          # {"status":"ok"}

# 2) HTTP 有沒有轉 HTTPS
curl -sI http://management.example.com/ | head -1           # 301

# 3) ★ WebSocket 升級有沒有穿過 nginx（要 --http1.1）
curl -sk --http1.1 -i -m 5 \
  -H 'Connection: Upgrade' -H 'Upgrade: websocket' \
  -H 'Sec-WebSocket-Version: 13' -H 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==' \
  -H 'Authorization: Bearer 隨便填' \
  https://management.example.com/api/agent/screen/ws | head -1
# 要看到：HTTP/1.1 101 Switching Protocols
# 若是 400／502，就是步驟 2／3 的 Upgrade、Connection 標頭沒設對
```

最後用瀏覽器開 `https://management.example.com` 登入，隨便開一台端點的「檢視畫面」
——看得到即時畫面就代表整條路徑（HTTPS + WebSocket）都通了。
**Agent 安裝包裡的 Server URL 就填這個網址。**

### 7.3 自簽憑證的後果（一定要處理）

> ⚠️ 自簽憑證瀏覽器會警告，而且 **Agent 的 `wss` 連線會因憑證不受信任而直接失敗**
> ——端點會一直顯示離線。內網部署請把 `/etc/ssl/eem/fullchain.pem`（或改用內部 CA
> 簽發的根憑證）**匯入端點與管理者機器的「受信任的根憑證授權」**，可用網域 GPO
> 派送。

### 7.4 只跑單一行程

> ⚠️ 即時畫面的轉發樞紐與錄影器都在「同一個行程的記憶體」裡。systemd 服務就是單
> 行程（`python wsgi.py`，多執行緒）。`proxy_pass` 指向單一 `127.0.0.1:5000` 即可。
> 若要多台後端，得做黏著路由（ip_hash／sticky）或共享 broker，速率限制也要改指向
> Redis，否則 Agent 與檢視者可能落在不同行程而彼此看不到。

進階題目（只開放內網來源、多站台、HTTP/2 細節、更多疑難排解）見
[`nginx-ubuntu.md`](nginx-ubuntu.md)。

---

## 8. 選配功能

- **螢幕錄影／截圖**：`ffmpeg` 安裝腳本已裝好，程式自動找得到；加密金鑰未設時
  會自動產生在 `instance/recording.key`（0600）。裝好就能在「錄影」頁設政策。
  ⚠️ **金鑰要備份**——遺失＝既有錄影／截圖無法解密。
- **NAS 儲存（FTP/SMB）**：主控台「儲存」頁建立目標並「測試連線」。錄影政策可各自
  選存到哪個目標，截圖存到標記「預設」的目標。（不設就存本機。）
- **MSI 安裝包產生 → 見 §8.1**。簡短版：**Linux 伺服器不能建 MSI**，要在一台
  Windows 上建。
- **保留期限清理**：已內建每小時自動清理逾期錄影／截圖。要外部排程可用
  `sudo -u eem /opt/eem/server/.venv/bin/python -m flask --app wsgi sweep-recordings`（cron）。
- **把錄影拿出來（下載 MP4）**：NAS／磁碟上的 `.mp4.enc` 是加密檔，直接開不了。
  請在主控台「回放」視窗按「⬇ 下載」，勾選要的片段（可 Shift 選一段範圍），伺服器
  會解密後打包成 ZIP（每段一個 MP4 ＋ `manifest.json`）；每次下載都記
  `EXPORT_RECORDING` 稽核。一次最多 300 段。

### 8.1 端點安裝包（MSI）—— 必須在 Windows 上建

**伺服器跑 Linux 時，主控台的「安裝包」頁不能用**，這不是設定問題，是 WiX 的限制：

```
$ wix --version
wix.exe : warning WIX0000: The WiX Toolset only supports Windows.
          All behavior after this point is undefined.
$ wix build EndpointAgent.wxs ...
EndpointAgent.wxs(92) : error WIX0389: The Directory/@Name attribute's value,
                        'Endpoint Management Agent', is not a relative path.
```

（實測於 Ubuntu + .NET SDK + WiX 5.0.2：工具裝得起來，但建置一定失敗。所以
**不要**再照舊訊息去跑 `dotnet tool install --global wix`——那條路走不通。
主控台現在會直接顯示這件事。）

**改用這個流程**（伺服器仍然是 Linux，只借一台 Windows 當建置機）：

| # | 在哪 | 做什麼 |
| --- | --- | --- |
| 1 | 主控台（Linux） | 「安裝包 → 註冊憑證」建立一張憑證，複製憑證字串。整個車隊共用就設**不限次數**；退役時記得撤銷 |
| 2 | Windows 建置機 | 安裝 .NET SDK 與 WiX：`dotnet tool install --global wix --version 5.*`、`wix extension add -g WixToolset.Util.wixext/5.0.2`、`wix extension add -g WixToolset.Firewall.wixext/5.0.2` |
| 3 | Windows 建置機 | 取得專案原始碼，在 `agent\` 執行 `.\build.ps1`（產生 `publish\EndpointAgent.exe`，只在 agent 程式碼改動時才需要重跑） |
| 4 | Windows 建置機 | 建 MSI：見下方指令 |
| 5 | 端點 | `msiexec /i <你的>.msi /qn`，或用 GPO／SCCM 派送 |

```powershell
cd agent\packaging
.\New-AgentMsi.ps1 `
    -ServerUrl https://management.example.com `
    -EnrollmentToken '主控台複製來的憑證字串' `
    -AdminPassword 'ITonly-2026' `
    -Label 台北辦公室
# → 台北辦公室.msi（會印出大小與 SHA256）
```

`New-AgentMsi.ps1` 做的事和伺服器的 `app/services/packaging.py` **完全一樣**：把
伺服器網址、註冊憑證、管理密碼的 PBKDF2 雜湊（210,000 次、SHA-256）包進 MSI。
明文密碼不進安裝包、不進資料庫、不進記錄。

- `-AdminPassword` 建議一定要給：沒有它，端點上就**不能**用
  `EndpointAgent.exe set-server` / `reset-enrollment` 改設定（agent 會拒絕），
  也無法用密碼保護解除安裝。
- 要簽章就加 `-CertPath C:\certs\eem.pfx`（密碼放環境變數 `EEM_SIGNING_PASSWORD`）。
  簽章用 `Set-AuthenticodeSignature`，**同樣只能在 Windows 執行**；未簽章的 MSI
  可能被 SmartScreen／AppLocker 擋。
- 同一張憑證可以重複建包；換伺服器網址就重建一份，或在端點上跑
  `EndpointAgent.exe set-server https://新網址`（需要管理密碼）。

安裝腳本仍然會把 `agent/publish`、`agent/packaging` 等產物複製到 `/opt/eem/agent`
——留著是為了日後若把伺服器搬回 Windows 可以直接用，Linux 上用不到。

---

## 9. 手動安裝（非 Debian 系，或不想用腳本）

腳本做的事等價於下列步驟；把套件管理器換成你的發行版即可（`dnf` 等）。

```bash
# 1) 系統套件
sudo dnf install -y python3 python3-pip ffmpeg nginx      # 範例：RHEL 系

# 2) 程式碼與虛擬環境
sudo mkdir -p /opt/eem && sudo cp -r server /opt/eem/
cd /opt/eem/server
sudo python3 -m venv .venv
sudo .venv/bin/pip install -U pip -r requirements.txt

# 3) .env（自動填入隨機密鑰）
sudo tee .env >/dev/null <<EOF
EEM_SECRET_KEY=$(python3 -c "import secrets;print(secrets.token_urlsafe(64))")
EEM_DATABASE_URI=sqlite:///eem.db
EEM_TRUST_PROXY_HEADERS=1
EEM_LOG_LEVEL=INFO
EOF
sudo chmod 640 .env

# 4) 資料庫 + 第一個最高管理員（會提示輸入密碼）
sudo .venv/bin/python -m flask --app wsgi init-db
sudo EEM_BOOTSTRAP_SECRET=$(python3 -c "import secrets;print(secrets.token_urlsafe(32))") \
     .venv/bin/python -m flask --app wsgi bootstrap-super-admin --username admin

# 5) 服務帳號與權限
sudo useradd --system --home-dir /opt/eem --shell /usr/sbin/nologin eem
sudo chown -R root:eem /opt/eem/server && sudo chmod -R go-w /opt/eem/server
sudo chown -R eem:eem /opt/eem/server/instance

# 6) 部署前檢查 + 前景試跑（監聽 127.0.0.1:5000）
sudo .venv/bin/python -m flask --app wsgi check-config
sudo -u eem .venv/bin/python wsgi.py
```

systemd 服務（`/etc/systemd/system/eem.service`）：

```ini
[Unit]
Description=螢幕測錄系統 管理伺服器
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=eem
Group=eem
WorkingDirectory=/opt/eem/server
ExecStart=/opt/eem/server/.venv/bin/python /opt/eem/server/wsgi.py
Restart=always
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/opt/eem/server/instance

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now eem
```

反向代理設定照抄 [`nginx-ubuntu.md`](nginx-ubuntu.md)。

---

## 10. 備份

至少備份三樣（停機或低峰時）：

| 對象 | 內容 |
| --- | --- |
| 資料庫 | SQLite：`/opt/eem/server/instance/eem.db`；PostgreSQL：`pg_dump` |
| `instance/` | 加密的錄影／截圖、`recording.key`、產生的安裝包 |
| `.env` | 密鑰（尤其 `EEM_SECRET_KEY`、`EEM_RECORDING_KEY`）。**遺失錄影金鑰＝既有錄影無法解密** |

最小可用備份：

```bash
sudo systemctl stop eem
sudo tar -czf /root/eem-backup-$(date +%F).tar.gz -C /opt/eem/server .env instance
sudo systemctl start eem
```

---

## 11. 疑難排解

### 11.1 瀏覽器打不開主控台（最常見）

**先在伺服器上貼這一段**，它會直接告訴你該用哪個網址、卡在哪一層：

```bash
sudo bash <<'DIAG'
echo "== 這台機器的網址（外面就用這個連）=="
ip -4 -o addr show scope global | awk '{split($4,a,"/"); print "   https://" a[1]}'

echo "== 服務 =="
printf '   eem   : %s\n' "$(systemctl is-active eem)"
printf '   nginx : %s\n' "$(systemctl is-active nginx)"

echo "== 監聽的埠（要看到 443 與 5000）=="
ss -lnt | awk 'NR>1 {print "   " $4}' | grep -E ':(80|443|5000)$' || echo "   沒有東西在聽 80/443/5000"

echo "== 站台 server_name =="
grep -hs server_name /etc/nginx/sites-enabled/* || echo "   沒有啟用的 nginx 站台"

echo "== 本機自測 =="
printf '   後端  : %s\n' "$(curl -s  --max-time 5 http://127.0.0.1:5000/api/health || echo '沒有回應')"
printf '   nginx : %s\n' "$(curl -sk --max-time 5 https://127.0.0.1/api/health     || echo '沒有回應')"

echo "== 防火牆 =="
if command -v ufw >/dev/null; then ufw status | head -3; else echo "   未安裝 ufw"; fi
DIAG
```

**一切正常時長這樣**（拿你的輸出跟它比）：

```
== 這台機器的網址（外面就用這個連）==
   https://192.168.1.50            ← 就用這個開瀏覽器
== 服務 ==
   eem   : active
   nginx : active
== 監聽的埠（要看到 443 與 5000）==
   127.0.0.1:5000                  ← 後端只在回環，正確
   0.0.0.0:443                     ← nginx 對外，正確
   0.0.0.0:80
== 站台 server_name ==
    server_name _;                 ← 沒指定網域時是 _（接受任何主機名）
== 本機自測 ==
   後端  : {"status":"ok"}
   nginx : {"status":"ok"}
```

依結果對照：

| 現象 | 原因與處理 |
| --- | --- |
| **兩個本機自測都回 `{"status":"ok"}`**，但外面連不到 | 網址或網路的問題，不是程式：①`management.example.com` 是文件範例，要換成你的網域或**直接用上面印出的 `https://<IP>`**；②VM 用 NAT 網路時，宿主機連不到 VM——改成**橋接（Bridged）**或設 443 的**通訊埠轉送**；③`ufw` 沒放行（`sudo ufw allow 'Nginx Full'`） |
| 後端 OK、nginx 那行沒回應 | nginx 沒裝或沒啟用站台。用 `--no-proxy` 裝的就照 §7.2 自己架，或重跑 `sudo bash install.sh`（會補上 nginx） |
| 兩個都沒回應、`eem` 不是 active | 服務沒起來：`journalctl -u eem -n 50` |
| 瀏覽器顯示「連線不安全／不是私人連線」 | **這是自簽憑證的正常行為**，不是壞掉。點「進階 → 繼續前往」即可；要根治見 §7.3 |
| 打 `http://` 沒反應 | 本站只在 443 提供服務，80 只做轉址。網址請用 `https://` 開頭 |
| 網域打不開但 IP 可以 | DNS 沒指向這台機器。用 `nslookup <你的網域>` 確認 A 記錄；內網測試可先在自己電腦的 hosts 檔加一行 `<VM 的 IP>  <你的網域>` |

> 判斷口訣：**本機 `curl` 通 = 程式沒問題**，剩下的都是網址、DNS、VM 網路模式或防火牆。

### 11.2 其他

| 症狀 | 可能原因與處理 |
| --- | --- |
| 安裝中途失敗 | 修正原因後**直接重跑腳本**，已完成的步驟會沿用，資料不會遺失 |
| 服務起不來 | `journalctl -u eem -n 50`。常見：`.env` 少了 `EEM_SECRET_KEY`、`instance/` 權限不對（`sudo chown -R eem:eem /opt/eem/server/instance`） |
| 主控台開得起來但**即時畫面卡住／WS 連不上** | 反向代理沒轉發 `Upgrade`/`Connection` 標頭；或跑了多 worker（要單行程） |
| 端點顯示**離線**但 Agent 有裝 | Agent 的 Server URL 錯、TLS 憑證不受信任（自簽沒匯入）、或憑證被撤銷需重新註冊；查 Agent `status` |
| 稽核來源 IP 都是同一個 | `.env` 沒設 `EEM_TRUST_PROXY_HEADERS=1`（設完要重啟） |
| 「錄影」頁提示未啟用 | 找不到 FFmpeg 或沒有加密金鑰；跑 `check-config` 看細節 |
| 「安裝包」頁說「此伺服器是 Linux，無法建置 MSI」 | **正常**，不是壞掉。WiX 只支援 Windows，請照 §8.1 在 Windows 上建 |
| 一般管理員看不到某些頁 | **這是預設行為**。最高管理員到「管理員 → 功能授權」逐項開放 |
| `502 Bad Gateway` | 後端沒起來：`systemctl status eem`、`curl 127.0.0.1:5000/api/health` |

---

## 12. 上線前安全檢查清單

- [ ] `sudo -u eem /opt/eem/server/.venv/bin/python -m flask --app wsgi check-config` 無 PROBLEM
- [ ] 憑證不是自簽的（或內部 CA 憑證已派送到所有端點）
- [ ] `EEM_TRUST_PROXY_HEADERS=1`，且前方就是自己那台 nginx
- [ ] 正式資料量改用 **PostgreSQL**
- [ ] 只跑**單一行程**（或已設定黏著路由）
- [ ] 已排定 `.env` / 資料庫 / `instance/` 的**備份**，`recording.key` 已另存
- [ ] 安裝時印出的管理員密碼已妥善保存，或已在主控台改過
- [ ] 螢幕監控／錄影的**書面同意**已就緒（見 `CLAUDE.md` §2、§14.4）
