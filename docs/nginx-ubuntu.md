# Ubuntu + nginx 反向代理教學

> **多數情況你不需要這一篇**：`server/install.sh` 已經自動裝好 nginx、憑證與
> WebSocket 轉發（見 [`deployment.md`](deployment.md) §1）。本文是給要**自訂**
> 設定的人：多站台、只開放內網、換憑證、或想知道每一行在做什麼。

讓 nginx 在螢幕測錄系統的管理伺服器前面負責 **HTTPS/TLS**，並正確轉發 **WebSocket**
（即時畫面、螢幕牆、Agent 回連都靠它）。本文以 Ubuntu 22.04／24.04 為準；
Windows 版見 [`nginx-windows.md`](nginx-windows.md)。

```
瀏覽器 / Agent ──HTTPS(443)──▶ nginx ──http(127.0.0.1:5000)──▶ EEM (gunicorn/wsgi)
                （TLS 在這裡終結）        （只在本機回環，不對外）
```

---

## 0. 前提

- 管理伺服器已依 `deployment.md` 跑在 **`127.0.0.1:5000`**（`install.sh` 會註冊成
  systemd 服務常駐；不想要腳本的 nginx 就用 `--no-proxy` 安裝）。
  先確認：`curl -s http://127.0.0.1:5000/api/health` 回 `{"status":"ok"}`。
- 你有一個要對外的網域，例如 `management.example.com`（下文請自行替換）。
  內網無公開網域也可以，用自簽憑證（見 §3B）。

---

## 1. 安裝 nginx

```bash
sudo apt update
sudo apt install -y nginx
```

---

## 2. 建立站台設定

寫入 `/etc/nginx/sites-available/eem`（把 `management.example.com` 換成你的網域；
憑證路徑在 §3 產生）：

```nginx
# WebSocket 升級用的對應（整台 nginx 只需定義一次；多站台時請移到 conf.d/）
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}

# 80 → 443：一律轉 HTTPS
server {
    listen 80;
    listen [::]:80;
    server_name management.example.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name management.example.com;

    # 憑證（§3 產生）
    ssl_certificate     /etc/ssl/eem/fullchain.pem;
    ssl_certificate_key /etc/ssl/eem/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;

    # 本程式在代理後方看不到 TLS，HSTS 由 nginx 送
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;

    # 截圖上傳最大約 25MB，留點餘裕
    client_max_body_size 32m;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_http_version 1.1;

        # WebSocket（即時畫面 / 螢幕牆 / Agent 的 /api/.../screen/ws）
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;

        # 轉發原始主機與來源資訊（供稽核來源 IP）
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # 螢幕串流是長連線，別中途被切
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }
}
```

> 為何要 `Upgrade` / `Connection` 兩個標頭：沒有它們，`/api/.../screen/ws` 的
> WebSocket 升級會失敗，畫面會一直卡在「已連線，等待畫面」。

---

## 3. 取得 TLS 憑證（二選一）

先建放憑證的目錄：`sudo mkdir -p /etc/ssl/eem`

### 3A. 公開網域 → Let's Encrypt（免費、自動續期，推薦）

```bash
sudo apt install -y certbot python3-certbot-nginx
# 先讓上面的站台生效（見 §4 啟用 + reload），再執行：
sudo certbot --nginx -d management.example.com
```

certbot 會自動改好 TLS 並設定**自動續期**。若你想沿用本文的設定檔，改用
`certbot certonly --nginx -d management.example.com`，憑證會在
`/etc/letsencrypt/live/management.example.com/`，把 §2 的兩行 `ssl_certificate*`
指到 `fullchain.pem` 與 `privkey.pem` 即可。

### 3B. 內網／無公開網域 → 自簽憑證

```bash
sudo openssl req -x509 -nodes -days 825 -newkey rsa:2048 \
  -keyout /etc/ssl/eem/privkey.pem \
  -out    /etc/ssl/eem/fullchain.pem \
  -subj   "/CN=management.example.com" \
  -addext "subjectAltName=DNS:management.example.com"
sudo chmod 600 /etc/ssl/eem/privkey.pem
```

> ⚠️ 自簽憑證瀏覽器會警告、**Agent 的 wss 連線會因憑證不受信任而失敗**。內網部署
> 請把這張憑證（或改用公司內部 CA 簽發）**匯入端點與管理者機器的「受信任的根憑證
> 授權」**（可用網域 GPO 派送）。SAN（`subjectAltName`）一定要有，否則新版瀏覽器
> 直接拒絕。

---

## 4. 啟用站台

```bash
sudo ln -s /etc/nginx/sites-available/eem /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default   # 移除預設歡迎頁
sudo nginx -t                                 # 檢查設定語法
sudo systemctl reload nginx
```

---

## 5. 防火牆

```bash
sudo ufw allow 'Nginx Full'     # 開放 80 + 443
sudo ufw enable                 # 若尚未啟用
```

**不要**對外開放 `5000` —— EEM 只監聽本機回環，由 nginx 從內部連它。

---

## 6. 讓 EEM 信任代理標頭

在 EEM 的 `.env` 設 `EEM_TRUST_PROXY_HEADERS=1`（這樣稽核的來源 IP 才是真正的
用戶端，而不是 nginx 的位址），然後重啟服務：

```bash
sudo systemctl restart eem       # 你的 EEM systemd 服務名稱
```

> 只有在**確定前面就是這台 nginx**（會覆寫 `X-Forwarded-For`）時才設 1；直接對外
> 時設 1 會讓來源 IP 可被偽造。

---

## 7. 驗證

```bash
# 自簽憑證用 -k 略過驗證；Let's Encrypt 可拿掉 -k
curl -k https://management.example.com/api/health      # {"status":"ok"}
```

1. 瀏覽器開 `https://management.example.com` → 用最高管理員登入。
2. 隨便開一台端點的「檢視畫面」→ 應看到即時畫面（代表 WebSocket 有穿過 nginx）。
3. **Agent 安裝包裡的 Server URL 就填** `https://management.example.com`。

---

## 8. 常見問題

| 症狀 | 原因與處理 |
| --- | --- |
| `502 Bad Gateway` | 後端沒起來。查 `systemctl status eem`、`curl 127.0.0.1:5000/api/health` |
| 即時畫面卡在「等待畫面」／WS 連不上 | `map`／`Upgrade`／`Connection` 標頭沒設對；改好後 `nginx -t && systemctl reload nginx` |
| `413 Request Entity Too Large`（截圖存不了） | 調高 `client_max_body_size` |
| 瀏覽器一直憑證警告 | 自簽憑證未受信任 → 匯入 CA，或改用內部 CA／Let's Encrypt |
| 端點顯示離線 / Agent 連不上（自簽） | 端點沒信任你的憑證 → 匯入受信任根憑證（GPO 派送） |
| 稽核來源 IP 都一樣 | `.env` 沒設 `EEM_TRUST_PROXY_HEADERS=1`（且重啟服務） |
| 大型 MSI 下載被中斷 | 於 `location /` 內加 `proxy_buffering off;`（下載走串流較穩） |

---

## 9. 進階（選配）

- **只允許內網來源連管理台**：在 `server {}`（443）內加
  `allow 10.0.0.0/8; deny all;`（依你的內網網段）。Agent 若在同網段一起放行。
- **HTTP/2**：上面已用 `listen 443 ssl http2;`（Ubuntu 22.04／24.04 皆適用；
  nginx 1.25+ 可改成獨立的 `http2 on;`）。
- **憑證續期測試**（Let's Encrypt）：`sudo certbot renew --dry-run`。
- **單一後端**：EEM 的即時畫面樞紐在單一行程內，`proxy_pass` 指向單一
  `127.0.0.1:5000` 即可；要多台時需在 upstream 做**黏著（ip_hash / sticky）**，
  否則 Agent 與檢視者可能落在不同後端而看不到彼此。
```
