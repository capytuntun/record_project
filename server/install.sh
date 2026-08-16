#!/usr/bin/env bash
#
# 螢幕測錄系統（管理伺服器）—— Linux 一行指令安裝。
#
#   sudo bash install.sh
#   sudo bash install.sh --domain eem.example.com --email me@example.com
#   curl -fsSL https://HOST/install.sh | sudo bash -s -- --source https://HOST/eem.tar.gz
#
# 一次做完：系統套件 → 虛擬環境 → .env 密鑰 → 建資料庫 → 建最高管理員 →
# systemd 服務 → nginx + HTTPS。
#
# 重跑 = 就地升級：程式碼換新，`.env` 與 `instance/`（資料庫、加密金鑰、錄影、
# 截圖、安裝包）一律保留。
#
set -Eeuo pipefail

INSTALL_ROOT=/opt/eem
SERVICE_USER=eem
SERVICE_NAME=eem
ADMIN_USER=admin
DOMAIN=""
LE_EMAIL=""
SOURCE=""
MODE=install
SETUP_PROXY=1
INSTALL_FFMPEG=1
PURGE=0

# wsgi.py 固定監聽回環的這個位址；nginx 從內部連它，不對外開放。
BIND_HOST=127.0.0.1
BIND_PORT=5000

TMPDIRS=()
NEW_ADMIN_PASSWORD=""
CERT_MODE=""

if [[ -t 1 ]]; then
    BOLD=$'\e[1m'; RED=$'\e[31m'; GREEN=$'\e[32m'; YELLOW=$'\e[33m'; DIM=$'\e[2m'; RESET=$'\e[0m'
else
    BOLD=""; RED=""; GREEN=""; YELLOW=""; DIM=""; RESET=""
fi

step() { printf '\n%s▶ %s%s\n' "$BOLD" "$*" "$RESET"; }
info() { printf '   %s\n' "$*"; }
ok()   { printf '   %s✓%s %s\n' "$GREEN" "$RESET" "$*"; }
warn() { printf '   %s!%s %s\n' "$YELLOW" "$RESET" "$*"; }
die()  { printf '\n%s✗ %s%s\n' "$RED" "$*" "$RESET" >&2; exit 1; }

cleanup() {
    local dir
    for dir in "${TMPDIRS[@]:-}"; do
        if [[ -n "$dir" && -d "$dir" ]]; then rm -rf "$dir"; fi
    done
    return 0
}
trap cleanup EXIT
trap 'die "安裝中止（第 $LINENO 行失敗）。上面最後一則訊息通常就是原因。"' ERR

usage() {
    cat <<'USAGE'
螢幕測錄系統 Linux 安裝程式

用法：
  sudo bash install.sh [選項]

常用：
  --domain <網域>     對外網域，例：eem.example.com（會設定 nginx + HTTPS）
  --email <信箱>      搭配 --domain 用 Let's Encrypt 申請正式憑證；不給則自簽
  --source <來源>     程式碼來源：目錄、.tar.gz 網址、或 .git 網址
                      （不給就用本腳本所在的原始碼）

其他：
  --dir <路徑>        安裝位置，預設 /opt/eem
  --user <帳號>       服務執行帳號，預設 eem
  --admin <帳號>      第一個最高管理員的使用者名稱，預設 admin
  --no-proxy          不安裝 nginx（伺服器只在 127.0.0.1:5000，自行接代理）
  --no-ffmpeg         不安裝 FFmpeg（不需要螢幕錄影時）
  --uninstall         停用並移除服務（保留 /opt/eem 的資料）
  --uninstall --purge 連同程式、資料庫、錄影一併刪除
  -h, --help          顯示本說明

環境變數：
  EEM_ADMIN_PASSWORD  指定最高管理員密碼（不給則自動產生並印出一次）

重跑本腳本即為升級：程式碼換新，.env 與 instance/ 保留。
USAGE
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dir)             INSTALL_ROOT=${2:?--dir 需要路徑}; shift 2 ;;
        --user)            SERVICE_USER=${2:?--user 需要帳號}; shift 2 ;;
        --admin)           ADMIN_USER=${2:?--admin 需要帳號}; shift 2 ;;
        --domain)          DOMAIN=${2:?--domain 需要網域}; shift 2 ;;
        --email)           LE_EMAIL=${2:?--email 需要信箱}; shift 2 ;;
        --source)          SOURCE=${2:?--source 需要目錄或網址}; shift 2 ;;
        --no-proxy)        SETUP_PROXY=0; shift ;;
        --no-ffmpeg)       INSTALL_FFMPEG=0; shift ;;
        --uninstall)       MODE=uninstall; shift ;;
        --purge)           PURGE=1; shift ;;
        -h|--help)         usage; exit 0 ;;
        *)                 die "未知參數：$1（用 --help 看用法）" ;;
    esac
done

APP_DIR="$INSTALL_ROOT/server"
VENV_PY="$APP_DIR/.venv/bin/python"
UNIT_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
NGINX_SITE="/etc/nginx/sites-available/eem"
CERT_DIR=/etc/ssl/eem

# ---------------------------------------------------------------- 前置檢查

require_root() {
    [[ ${EUID:-$(id -u)} -eq 0 ]] || die "請用 root 執行：sudo bash install.sh"
}

require_apt() {
    command -v apt-get >/dev/null 2>&1 || die \
        "本腳本支援 Debian／Ubuntu 系（apt）。其他發行版請參考 docs/deployment.md 的「手動安裝」。"
}

# ---------------------------------------------------------------- 解除安裝

do_uninstall() {
    step "移除螢幕測錄系統服務"
    # 用檔案／systemctl cat 判斷，不要用 `… | grep -q`：grep 命中就關閉管線，
    # 上游收到 SIGPIPE，在 pipefail 下整條管線會被判定失敗。
    if [[ -f "$UNIT_FILE" ]] || systemctl cat "${SERVICE_NAME}.service" >/dev/null 2>&1; then
        systemctl disable --now "$SERVICE_NAME" >/dev/null 2>&1 || true
        rm -f "$UNIT_FILE"
        systemctl daemon-reload
        ok "已停用並移除 systemd 服務"
    else
        info "沒有找到 ${SERVICE_NAME}.service"
    fi

    if [[ -e /etc/nginx/sites-enabled/eem ]]; then
        rm -f /etc/nginx/sites-enabled/eem
        nginx -t >/dev/null 2>&1 && systemctl reload nginx || true
        ok "已停用 nginx 站台（設定檔保留在 $NGINX_SITE）"
    fi

    if [[ $PURGE -eq 1 ]]; then
        warn "--purge：將刪除 $INSTALL_ROOT（含資料庫、錄影、加密金鑰）"
        rm -rf "$INSTALL_ROOT"
        rm -f "$NGINX_SITE" /etc/nginx/conf.d/eem-websocket.conf
        rm -rf "$CERT_DIR"          # 本腳本產生的自簽憑證（Let's Encrypt 的不在這）
        id -u "$SERVICE_USER" >/dev/null 2>&1 && userdel "$SERVICE_USER" 2>/dev/null || true
        ok "已刪除 $INSTALL_ROOT、自簽憑證與服務帳號"
    else
        info "資料保留在 $INSTALL_ROOT（要一併刪除請加 --purge）"
    fi
    printf '\n%s解除安裝完成。%s\n' "$GREEN" "$RESET"
}

# ---------------------------------------------------------------- 取得原始碼

find_server_dir() {
    # 在來源樹裡找出含 wsgi.py 的 server 目錄。
    local root=$1 candidate
    for candidate in "$root" "$root/server"; do
        [[ -f "$candidate/wsgi.py" && -f "$candidate/requirements.txt" ]] && { printf '%s' "$candidate"; return 0; }
    done
    # `find | head` 會讓 find 收到 SIGPIPE，pipefail 下要自行吞掉非 0 結束碼。
    candidate=$(find "$root" -maxdepth 3 -name wsgi.py -printf '%h\n' 2>/dev/null | head -n1) || candidate=""
    [[ -n "$candidate" && -f "$candidate/requirements.txt" ]] && { printf '%s' "$candidate"; return 0; }
    return 1
}

resolve_source() {
    step "取得程式碼"
    local root=""

    if [[ -z "$SOURCE" ]]; then
        # 沒指定來源：用本腳本所在的原始碼（curl | bash 時取不到，需 --source）。
        local self="${BASH_SOURCE[0]:-}"
        if [[ -n "$self" && -f "$self" ]]; then
            root=$(cd -- "$(dirname -- "$self")" && pwd)
        else
            die "從管線執行時無法得知程式碼位置，請加 --source <目錄或 .tar.gz 網址>"
        fi
    else
        case "$SOURCE" in
            http://*|https://*)
                local tmp; tmp=$(mktemp -d); TMPDIRS+=("$tmp")
                if [[ "$SOURCE" == *.git ]]; then
                    command -v git >/dev/null 2>&1 || apt_install git
                    info "git clone $SOURCE"
                    git clone --depth 1 --quiet "$SOURCE" "$tmp/src"
                else
                    info "下載 $SOURCE"
                    command -v curl >/dev/null 2>&1 || apt_install curl ca-certificates
                    curl -fsSL "$SOURCE" -o "$tmp/src.tar.gz"
                    mkdir -p "$tmp/src"
                    tar -xzf "$tmp/src.tar.gz" -C "$tmp/src"
                fi
                root="$tmp/src"
                ;;
            *.tar.gz|*.tgz)
                # 從 Windows 傳過來的壓縮檔，不用先自己解。
                [[ -f "$SOURCE" ]] || die "找不到壓縮檔：$SOURCE"
                local tmp; tmp=$(mktemp -d); TMPDIRS+=("$tmp")
                info "解壓 $SOURCE"
                mkdir -p "$tmp/src"
                tar -xzf "$SOURCE" -C "$tmp/src"
                root="$tmp/src"
                ;;
            *)
                [[ -d "$SOURCE" ]] || die "找不到來源目錄或壓縮檔：$SOURCE"
                root=$(cd -- "$SOURCE" && pwd)
                ;;
        esac
    fi

    SRC_SERVER=$(find_server_dir "$root") || die "在 $root 底下找不到 server/（需含 wsgi.py 與 requirements.txt）"
    SRC_ROOT=$(dirname "$SRC_SERVER")
    ok "程式碼來源：$SRC_SERVER"
}

# ---------------------------------------------------------------- 系統套件

apt_updated=0
apt_install() {
    (( apt_updated )) || { DEBIAN_FRONTEND=noninteractive apt-get update -qq; apt_updated=1; }
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends "$@" >/dev/null
}

install_system_packages() {
    step "安裝系統套件"
    local pkgs=(ca-certificates curl openssl rsync tar)
    if (( INSTALL_FFMPEG )); then pkgs+=(ffmpeg); fi
    if (( SETUP_PROXY )); then pkgs+=(nginx); fi
    info "${pkgs[*]}"
    apt_install "${pkgs[@]}"
    ok "系統套件就緒"
}

python_ok() {
    "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1
}

ensure_python() {
    step "準備 Python"
    local candidate
    PY=""
    for candidate in python3.13 python3.12 python3.11 python3; do
        if command -v "$candidate" >/dev/null 2>&1 && python_ok "$candidate"; then
            PY=$(command -v "$candidate"); break
        fi
    done

    if [[ -z "$PY" ]]; then
        info "系統預設 Python 低於 3.11，嘗試安裝較新版本…"
        for candidate in python3.12 python3.11; do
            if apt_install "$candidate" "${candidate}-venv" 2>/dev/null && command -v "$candidate" >/dev/null 2>&1; then
                PY=$(command -v "$candidate"); break
            fi
        done
    fi

    [[ -n "$PY" ]] || die "需要 Python 3.11 以上。Ubuntu 22.04 可執行：sudo apt install python3.11 python3.11-venv，或改用 Ubuntu 24.04／Debian 12。"

    # Debian 系把 venv 的 ensurepip 拆成獨立套件，且必須對應到選中的直譯器版本。
    # 只測 `-m venv --help` 會過關但實際建立時才炸，所以直接測 ensurepip。
    if ! "$PY" -c 'import ensurepip, venv' >/dev/null 2>&1; then
        local pyver; pyver=$("$PY" -c 'import sys; print("python3.%d" % sys.version_info[1])')
        info "補裝 ${pyver}-venv"
        apt_install "${pyver}-venv" 2>/dev/null || apt_install python3-venv 2>/dev/null || true
        "$PY" -c 'import ensurepip, venv' >/dev/null 2>&1 || \
            die "無法建立虛擬環境：缺少 ${pyver}-venv 套件，請先安裝：sudo apt install ${pyver}-venv"
    fi
    ok "使用 $($PY -V 2>&1)（$PY）"
}

# ---------------------------------------------------------------- 佈署檔案

ensure_service_user() {
    if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
        useradd --system --home-dir "$INSTALL_ROOT" --shell /usr/sbin/nologin "$SERVICE_USER"
        ok "建立服務帳號 $SERVICE_USER（不可登入）"
    fi
}

stop_service_if_running() {
    if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
        info "停止執行中的服務以進行升級"
        systemctl stop "$SERVICE_NAME"
    fi
}

sync_code() {
    step "佈署程式碼到 $APP_DIR"
    mkdir -p "$APP_DIR"
    # 排除的項目同時也不會被 --delete 刪掉，所以 .env 與 instance/ 在升級時安全。
    rsync -a --delete \
        --exclude '.venv/' \
        --exclude '.env' \
        --exclude 'instance/' \
        --exclude '__pycache__/' \
        --exclude '*.pyc' \
        --exclude '.pytest_cache/' \
        --exclude 'tools/ffmpeg/' \
        "$SRC_SERVER"/ "$APP_DIR"/
    ok "伺服器程式碼已同步"

    # 手冊跟著程式走，出事時在伺服器上就查得到。
    if [[ -d "$SRC_ROOT/docs" ]]; then
        mkdir -p "$INSTALL_ROOT/docs"
        rsync -a --delete "$SRC_ROOT/docs"/ "$INSTALL_ROOT/docs"/
    fi

    # Agent 產物：主控台的「安裝包」功能會在這些路徑找 EndpointAgent.exe 與 WiX 定義。
    local rel copied=0
    for rel in "agent/publish" "agent/packaging" "agent/signing" \
               "agent/src/EndpointAgent.CustomActions/bin/Release/net472"; do
        if [[ -d "$SRC_ROOT/$rel" ]]; then
            mkdir -p "$INSTALL_ROOT/$rel"
            rsync -a --delete "$SRC_ROOT/$rel"/ "$INSTALL_ROOT/$rel"/
            copied=1
        fi
    done
    if (( copied )); then
        ok "Agent 素材已同步（$INSTALL_ROOT/agent）"
    else
        # Linux 本來就不能建 MSI（WiX 只支援 Windows），所以這不是問題。
        info "來源沒有 agent/ 產物 —— Linux 不建 MSI，可忽略（見 docs/deployment.md §8.1）"
    fi
}

setup_venv() {
    step "建立虛擬環境並安裝相依套件"
    # 判斷依據是「venv 裡的 pip 能不能跑」，而不是檔案在不在：上次中斷留下的
    # 半成品 venv 有 bin/python 卻沒有 pip，沿用它會在下一步才炸。
    if ! "$VENV_PY" -m pip --version >/dev/null 2>&1; then
        rm -rf "$APP_DIR/.venv"
        "$PY" -m venv "$APP_DIR/.venv"
    fi
    if ! "$VENV_PY" -m pip --version >/dev/null 2>&1; then
        "$VENV_PY" -m ensurepip --default-pip >/dev/null 2>&1 || true
    fi
    "$VENV_PY" -m pip --version >/dev/null 2>&1 || \
        die "虛擬環境裡沒有 pip。請確認已安裝對應版本的 python3.X-venv 套件後重跑。"

    "$VENV_PY" -m pip install -q --upgrade pip
    "$VENV_PY" -m pip install -q -r "$APP_DIR/requirements.txt"
    ok "相依套件安裝完成"
}

# ---------------------------------------------------------------- 設定

gen_secret() { "$VENV_PY" -c "import secrets; print(secrets.token_urlsafe($1))"; }

gen_password() {
    # 需符合密碼政策：至少 12 碼、四類字元中至少三類。這裡固定給大寫＋小寫＋數字。
    "$VENV_PY" - <<'PY'
import secrets, string
pools = [string.ascii_lowercase, string.ascii_uppercase, string.digits]
chars = [secrets.choice(p) for p in pools]
alphabet = "".join(pools)
chars += [secrets.choice(alphabet) for _ in range(17)]
secrets.SystemRandom().shuffle(chars)
print("".join(chars))
PY
}

set_env_var() {
    local key=$1 value=$2 file="$APP_DIR/.env"
    if grep -q "^${key}=" "$file" 2>/dev/null; then
        sed -i "s|^${key}=.*|${key}=${value}|" "$file"
    else
        # 手改過的 .env 可能沒有結尾換行，直接附加會黏到最後一行上。
        if [[ -s "$file" && -n "$(tail -c1 "$file")" ]]; then printf '\n' >> "$file"; fi
        printf '%s=%s\n' "$key" "$value" >> "$file"
    fi
}

write_env() {
    step "設定環境檔 .env"
    if [[ -f "$APP_DIR/.env" ]]; then
        ok "沿用既有 .env（升級不會動它）"
    else
        ( umask 077
          cat > "$APP_DIR/.env" <<EOF
# 由 install.sh 產生。此檔含 JWT 主密鑰，請納入備份並妥善保護。
# 完整選項見 .env.example 與 app/config.py。

EEM_SECRET_KEY=$(gen_secret 64)
EEM_DATABASE_URI=sqlite:///eem.db
EEM_TRUST_PROXY_HEADERS=0
EEM_LOG_LEVEL=INFO
EOF
        )
        ok "已產生 .env（含隨機 EEM_SECRET_KEY）"
    fi
    # 安裝視窗（EEM_BOOTSTRAP_SECRET）只在建立管理員那一刻存在於記憶體，
    # 從不寫進檔案，因此不需要事後刪除。
    chown root:"$SERVICE_USER" "$APP_DIR/.env"
    chmod 640 "$APP_DIR/.env"
}

flask_cmd() { ( cd "$APP_DIR" && "$VENV_PY" -m flask --app wsgi "$@" ); }

init_database() {
    step "初始化資料庫"
    mkdir -p "$APP_DIR/instance"
    flask_cmd init-db >/dev/null
    ok "資料表已建立／補齊（instance/eem.db）"
}

create_admin() {
    step "建立第一個最高管理員"
    local password="${EEM_ADMIN_PASSWORD:-}" generated=0
    if [[ -z "$password" ]]; then password=$(gen_password); generated=1; fi

    # 安裝視窗：CLI 沒有這把密鑰就拒絕執行；用完立刻收回。
    export EEM_BOOTSTRAP_SECRET
    EEM_BOOTSTRAP_SECRET=$(gen_secret 32)
    local out rc=0
    out=$(printf '%s\n' "$password" | flask_cmd bootstrap-super-admin \
            --username "$ADMIN_USER" --password-stdin 2>&1) || rc=$?
    unset EEM_BOOTSTRAP_SECRET

    if (( rc == 0 )); then
        if (( generated )); then NEW_ADMIN_PASSWORD=$password; fi
        ok "已建立最高管理員 $ADMIN_USER"
    elif grep -qi "already exists" <<<"$out"; then
        ok "已有最高管理員，略過建立"
    else
        printf '%s\n' "$out" >&2
        die "建立最高管理員失敗"
    fi
}

fix_permissions() {
    # 程式碼 root 所有、服務帳號唯讀；只有 instance/ 可寫。
    chown -R root:"$SERVICE_USER" "$APP_DIR"
    # 來源可能來自 Windows 共用或 USB（那裡的檔案一律 777）。若照抄過來，
    # 任何本機使用者都能改寫這個以服務身分執行的程式碼，所以一律收回寫入權。
    chmod -R go-w "$APP_DIR"
    chmod 750 "$APP_DIR"
    chmod 640 "$APP_DIR/.env"
    mkdir -p "$APP_DIR/instance"
    # 以 root 執行 flask 會在 instance/ 產生 eem.db 與 recording.key，
    # 交回服務帳號後服務才讀得到（否則啟動時會 PermissionError）。
    chown -R "$SERVICE_USER":"$SERVICE_USER" "$APP_DIR/instance"
    chmod 750 "$APP_DIR/instance"
    if [[ -d "$INSTALL_ROOT/agent" ]]; then
        chown -R root:"$SERVICE_USER" "$INSTALL_ROOT/agent"
        chmod -R go-w "$INSTALL_ROOT/agent"
    fi
    if [[ -d "$INSTALL_ROOT/docs" ]]; then
        chown -R root:root "$INSTALL_ROOT/docs"
        chmod -R go-w "$INSTALL_ROOT/docs"
    fi
    chmod 755 "$INSTALL_ROOT"
}

# ---------------------------------------------------------------- 服務

install_service() {
    step "註冊 systemd 服務"
    cat > "$UNIT_FILE" <<EOF
[Unit]
Description=螢幕測錄系統 管理伺服器
Documentation=file://$INSTALL_ROOT/docs/deployment.md
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_USER
WorkingDirectory=$APP_DIR
Environment=PYTHONUNBUFFERED=1
# 單一行程，監聽 $BIND_HOST:$BIND_PORT。即時畫面的轉發樞紐與錄影器都在這個
# 行程的記憶體裡，多開 worker 會讓 Agent 與檢視者落在不同行程而看不到彼此。
ExecStart=$VENV_PY $APP_DIR/wsgi.py
Restart=always
RestartSec=3

# 加固：只有 instance/ 可寫，其餘檔案系統唯讀。
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$APP_DIR/instance
ProtectKernelTunables=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable --quiet "$SERVICE_NAME"
    systemctl restart "$SERVICE_NAME"
    ok "服務 ${SERVICE_NAME}.service 已啟用（開機自動啟動）"
}

wait_healthy() {
    step "驗證伺服器"
    local i body
    for i in $(seq 1 40); do
        body=$(curl -fsS "http://${BIND_HOST}:${BIND_PORT}/api/health" 2>/dev/null) || body=""
        if [[ "$body" == *'"ok"'* ]]; then
            ok "健康檢查通過：http://${BIND_HOST}:${BIND_PORT}/api/health"
            return 0
        fi
        sleep 1
    done
    warn "伺服器沒有在 40 秒內回應，最近的記錄："
    journalctl -u "$SERVICE_NAME" -n 30 --no-pager || true
    die "啟動失敗。修正後可直接重跑本腳本。"
}

# ---------------------------------------------------------------- nginx + TLS

primary_ip() {
    local addr
    addr=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{print $7; exit}') || addr=""
    if [[ -z "$addr" ]]; then
        addr=$(hostname -I 2>/dev/null | awk '{print $1}') || addr=""
    fi
    printf '%s' "$addr"
}

make_self_signed_cert() {
    local cn=$1 san=$2
    mkdir -p "$CERT_DIR"
    if [[ -f "$CERT_DIR/fullchain.pem" && -f "$CERT_DIR/privkey.pem" ]]; then
        info "沿用既有自簽憑證（$CERT_DIR）"
        return
    fi
    openssl req -x509 -nodes -days 825 -newkey rsa:2048 \
        -keyout "$CERT_DIR/privkey.pem" -out "$CERT_DIR/fullchain.pem" \
        -subj "/CN=$cn" -addext "subjectAltName=$san" >/dev/null 2>&1
    chmod 600 "$CERT_DIR/privkey.pem"
    ok "已產生自簽憑證（CN=$cn）"
}

setup_nginx() {
    step "設定 nginx 反向代理與 HTTPS"

    # 升級時不能踩掉別人的設定：certbot 會改寫這個站台檔（換成 Let's Encrypt 的
    # 憑證路徑），管理員也可能手動加了來源限制。凡是不再帶本腳本標記或已含
    # letsencrypt 的檔案，一律保留原樣，只確認站台有啟用。
    local preserve=0
    if [[ -f "$NGINX_SITE" ]]; then
        if grep -q "letsencrypt" "$NGINX_SITE" 2>/dev/null; then preserve=1; fi
        if ! grep -q "由 install.sh 產生" "$NGINX_SITE" 2>/dev/null; then preserve=1; fi
    fi
    if (( preserve )); then
        info "偵測到已被 certbot／人工修改過的站台設定，保留不覆寫"
        mkdir -p /etc/nginx/sites-enabled
        ln -sfn "$NGINX_SITE" /etc/nginx/sites-enabled/eem
        nginx -t >/dev/null 2>&1 || { nginx -t; die "nginx 設定檢查失敗"; }
        systemctl reload nginx
        set_env_var EEM_TRUST_PROXY_HEADERS 1
        systemctl restart "$SERVICE_NAME"
        CERT_MODE="preserved"
        ok "nginx 站台已啟用（沿用原設定）"
        return 0
    fi

    local server_name san cn
    if [[ -n "$DOMAIN" ]]; then
        server_name="$DOMAIN"; cn="$DOMAIN"; san="DNS:$DOMAIN"
    else
        server_name="_"; cn=$(hostname -f 2>/dev/null || hostname)
        san="DNS:$cn"
        local ipaddr; ipaddr=$(primary_ip)
        # 沒有網域時多半是用 IP 連線，憑證的 SAN 一定要含 IP，否則瀏覽器直接拒絕。
        if [[ -n "$ipaddr" ]]; then san="$san,IP:$ipaddr"; fi
    fi

    # nginx 1.25.1 起 listen 的 http2 參數改成獨立的 http2 指令；用錯版本會 -t 失敗。
    local listen_extra="" http2_line=""
    local nginx_ver; nginx_ver=$(nginx -v 2>&1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' || true)
    if [[ -n "$nginx_ver" ]] && printf '1.25.1\n%s\n' "$nginx_ver" | sort -V -C; then
        http2_line="    http2 on;"
    else
        listen_extra=" http2"
    fi

    make_self_signed_cert "$cn" "$san"
    CERT_MODE="self-signed"

    # WebSocket 升級對應放在 conf.d，整台 nginx 只需定義一次。
    cat > /etc/nginx/conf.d/eem-websocket.conf <<'EOF'
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}
EOF

    cat > "$NGINX_SITE" <<EOF
# 由 install.sh 產生。伺服器只監聽回環，對外一律經過這裡（TLS 在此終結）。
server {
    listen 80;
    listen [::]:80;
    server_name $server_name;
    return 301 https://\$host\$request_uri;
}

server {
    listen 443 ssl${listen_extra};
    listen [::]:443 ssl${listen_extra};
${http2_line}
    server_name $server_name;

    ssl_certificate     $CERT_DIR/fullchain.pem;
    ssl_certificate_key $CERT_DIR/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;

    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;

    # 截圖上傳上限約 25MB，留餘裕
    client_max_body_size 32m;

    location / {
        proxy_pass http://${BIND_HOST}:${BIND_PORT};
        proxy_http_version 1.1;

        # 即時畫面／螢幕牆／Agent 回連都靠這兩個標頭
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection \$connection_upgrade;

        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;

        # 螢幕串流是長連線
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;

        # 大型 MSI 直接串流下載
        proxy_buffering off;
    }
}
EOF

    mkdir -p /etc/nginx/sites-enabled
    ln -sfn "$NGINX_SITE" /etc/nginx/sites-enabled/eem
    # 沒有指定網域時本站是 catch-all，會和 nginx 預設歡迎頁衝突。
    if [[ -z "$DOMAIN" && -e /etc/nginx/sites-enabled/default ]]; then
        rm -f /etc/nginx/sites-enabled/default
    fi

    nginx -t >/dev/null 2>&1 || { nginx -t; die "nginx 設定檢查失敗"; }
    systemctl enable --quiet nginx 2>/dev/null || true
    systemctl restart nginx
    ok "nginx 已啟用（80 → 443，含 WebSocket 轉發）"

    # 代理會覆寫 X-Forwarded-For，稽核來源 IP 才會是真正的用戶端。
    set_env_var EEM_TRUST_PROXY_HEADERS 1
    systemctl restart "$SERVICE_NAME"

    if [[ -n "$DOMAIN" && -n "$LE_EMAIL" ]]; then
        info "向 Let's Encrypt 申請正式憑證…"
        if apt_install certbot python3-certbot-nginx && \
           certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos -m "$LE_EMAIL" --redirect >/dev/null 2>&1; then
            CERT_MODE="letsencrypt"
            ok "已取得 Let's Encrypt 憑證（自動續期已設定）"
        else
            warn "Let's Encrypt 申請失敗（DNS 未指向本機或 80 埠不通？），先沿用自簽憑證。"
            warn "修正後可重跑：sudo certbot --nginx -d $DOMAIN"
        fi
    fi

    # 只在防火牆已啟用時補規則，避免把自己關在門外。
    local ufw_status=""
    if command -v ufw >/dev/null 2>&1; then ufw_status=$(ufw status 2>/dev/null || true); fi
    if [[ "$ufw_status" == *"Status: active"* ]]; then
        ufw allow 'Nginx Full' >/dev/null 2>&1 || true
        ok "ufw 已放行 80／443"
    fi
}

# ---------------------------------------------------------------- 選配

run_check_config() {
    step "部署前檢查（flask check-config）"
    local out rc=0
    out=$(flask_cmd check-config 2>&1) || rc=$?
    printf '%s\n' "$out" | sed 's/^/   /'
    if (( rc != 0 )); then warn "有 PROBLEM 項目，請依上面訊息處理。"; fi
    # 這一輪同樣是以 root 執行 flask，確保沒有留下 root 所有的檔案。
    chown -R "$SERVICE_USER":"$SERVICE_USER" "$APP_DIR/instance"
}

print_summary() {
    local url
    if [[ -n "$DOMAIN" ]]; then
        url="https://$DOMAIN"
    elif (( SETUP_PROXY )); then
        url="https://$(primary_ip)"
    else
        url="http://${BIND_HOST}:${BIND_PORT}（只在本機，請自行接反向代理）"
    fi

    printf '\n%s══════════════════════════════════════════════════════════%s\n' "$GREEN" "$RESET"
    printf '%s 安裝完成%s\n' "$BOLD" "$RESET"
    printf '%s══════════════════════════════════════════════════════════%s\n\n' "$GREEN" "$RESET"
    printf '  主控台網址   %s\n' "$url"
    if [[ -n "$NEW_ADMIN_PASSWORD" ]]; then
        printf '  管理員帳號   %s\n' "$ADMIN_USER"
        printf '  管理員密碼   %s%s%s   %s← 只顯示這一次，請立刻保存%s\n' \
            "$BOLD" "$NEW_ADMIN_PASSWORD" "$RESET" "$YELLOW" "$RESET"
    else
        printf '  管理員帳號   %s（沿用既有密碼）\n' "$ADMIN_USER"
    fi
    printf '\n  安裝位置     %s\n' "$APP_DIR"
    printf '  設定檔       %s/.env\n' "$APP_DIR"
    printf '  資料與金鑰   %s/instance/（eem.db、recording.key、錄影、截圖）\n' "$APP_DIR"
    printf '\n  服務指令     systemctl status|restart|stop %s\n' "$SERVICE_NAME"
    printf '  查看記錄     journalctl -u %s -f\n' "$SERVICE_NAME"
    printf '  升級／修復   重跑本腳本（資料會保留）\n'
    printf '  解除安裝     sudo bash install.sh --uninstall\n'

    if [[ "$CERT_MODE" == "self-signed" ]]; then
        printf '\n  %s憑證是自簽的%s：瀏覽器會警告，且 %sAgent 的 wss 連線會因不受信任而失敗%s。\n' \
            "$YELLOW" "$RESET" "$BOLD" "$RESET"
        printf '  正式使用請用 --domain <網域> --email <信箱> 重跑，或把 %s/fullchain.pem\n' "$CERT_DIR"
        printf '  匯入端點的「受信任的根憑證授權」（可用 GPO 派送）。\n'
    fi
    printf '\n  %s請備份 %s/.env 與 %s/instance/%s —— 遺失金鑰＝既有錄影／截圖無法解密。\n\n' \
        "$YELLOW" "$APP_DIR" "$APP_DIR" "$RESET"
}

# ---------------------------------------------------------------- 主流程

require_root
require_apt

if [[ $MODE == uninstall ]]; then
    do_uninstall
    exit 0
fi

if (( PURGE )); then
    die "--purge 只能搭配 --uninstall 使用（它會刪除資料庫與錄影）。"
fi

printf '%s螢幕測錄系統 —— Linux 安裝%s\n' "$BOLD" "$RESET"
printf '%s安裝位置 %s ・ 服務帳號 %s%s\n' "$DIM" "$INSTALL_ROOT" "$SERVICE_USER" "$RESET"

resolve_source
install_system_packages
ensure_python
ensure_service_user
stop_service_if_running
sync_code
setup_venv
write_env
init_database
create_admin
fix_permissions
install_service
wait_healthy
if (( SETUP_PROXY )); then setup_nginx; fi
run_check_config
print_summary
