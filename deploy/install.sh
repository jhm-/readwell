#!/usr/bin/env bash
# Install or update Readwell on an Arch Linux (systemd) host. Safe to re-run: re-running updates.
#
#   git clone https://github.com/jhm-/readwell.git ~/readwell-src
#   sudo bash ~/readwell-src/deploy/install.sh
#
# Environment overrides: REPO_URL BRANCH APP_DIR DATA_DIR CONFIG_DIR NAS_MOUNT PORT
set -euo pipefail

REPO_URL=${REPO_URL:-https://github.com/jhm-/readwell.git}
BRANCH=${BRANCH:-master}
APP_DIR=${APP_DIR:-/opt/readwell}
DATA_DIR=${DATA_DIR:-/var/lib/readwell}
ETC_DIR=/etc/readwell
NAS_MOUNT=${NAS_MOUNT:-/mnt/Data}
CONFIG_DIR=${CONFIG_DIR:-$NAS_MOUNT/Readwell/config}
BACKUP_DIR=${BACKUP_DIR:-$NAS_MOUNT/Readwell/backups}
PORT=${PORT:-8080}
SVC_USER=readwell

step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33m  ! %s\033[0m\n' "$*"; }
die()  { printf '\033[31m  ✗ %s\033[0m\n' "$*" >&2; exit 1; }
as_svc() { sudo -u "$SVC_USER" -H "$@"; }

[[ $EUID -eq 0 ]] || die "run with sudo"
cd /  # commands run as the service user can't enter the caller's home directory

# ------------------------------------------------------------------ prerequisites
step "Checking prerequisites"
missing=()
for cmd in git uv curl ss; do command -v "$cmd" >/dev/null || missing+=("$cmd"); done
if ((${#missing[@]})); then
    # Not installed automatically: on Arch, installing single packages without a full -Syu is a partial upgrade.
    die "missing: ${missing[*]}. Install with:  sudo pacman -Syu --needed git uv curl iproute2"
fi
echo "  git, uv, curl, ss: ok"

step "Checking port $PORT"
if ss -ltnH "sport = :$PORT" | grep -q .; then
    if systemctl is-active --quiet readwell-web; then
        echo "  in use by readwell-web (update): ok"
    else
        ss -ltnpH "sport = :$PORT" || true
        die "port $PORT is taken by something else. Re-run with PORT=<free port>, and set server.port in config.yaml to match."
    fi
else
    echo "  $PORT is free"
fi

# ------------------------------------------------------------------ user and directories
step "Service user and directories"
if ! id "$SVC_USER" >/dev/null 2>&1; then
    useradd --system --home-dir "$DATA_DIR" --shell /usr/bin/nologin "$SVC_USER"
    echo "  created system user $SVC_USER"
else
    echo "  user $SVC_USER exists"
fi
install -d -o "$SVC_USER" -g "$SVC_USER" -m 750 "$DATA_DIR" "$DATA_DIR/cache"
install -d -o root -g "$SVC_USER" -m 750 "$ETC_DIR"

# ------------------------------------------------------------------ code
step "Code ($REPO_URL, branch $BRANCH)"
if [[ -d $APP_DIR/.git ]]; then
    as_svc git -C "$APP_DIR" fetch --quiet origin "$BRANCH"
    as_svc git -C "$APP_DIR" reset --quiet --hard "origin/$BRANCH"
    echo "  updated to $(as_svc git -C "$APP_DIR" log -1 --format='%h %s')"
else
    git clone --quiet --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
    chown -R "$SVC_USER:$SVC_USER" "$APP_DIR"
    echo "  cloned $(as_svc git -C "$APP_DIR" log -1 --format='%h %s')"
fi

# ------------------------------------------------------------------ python
step "Python environment (uv-managed Python, independent of the system Python)"
(
    cd "$APP_DIR"
    as_svc env UV_PYTHON_INSTALL_DIR="$DATA_DIR/python" UV_CACHE_DIR="$DATA_DIR/cache/uv" \
        UV_PYTHON_PREFERENCE=only-managed uv sync --frozen --no-dev --quiet
)
echo "  $("$APP_DIR/.venv/bin/python" --version), readwell $("$APP_DIR/.venv/bin/readwell" --version | cut -d' ' -f2)"

# ------------------------------------------------------------------ environment file
step "Environment file ($ETC_DIR/env)"
if [[ ! -f $ETC_DIR/env ]]; then
    cat >"$ETC_DIR/env" <<EOF
# Readwell service environment. Secrets go here, never in the YAML config.
READWELL_CONFIG_DIR=$CONFIG_DIR
READWELL_DATA_DIR=$DATA_DIR
READWELL_BACKUP_DIR=$BACKUP_DIR
# GEMINI_API_KEY=            # Phase 3
EOF
    chown root:"$SVC_USER" "$ETC_DIR/env"
    chmod 640 "$ETC_DIR/env"
    echo "  created"
else
    echo "  exists (left unchanged)"
fi

# ------------------------------------------------------------------ NAS config share
step "Config directory ($CONFIG_DIR)"
if as_svc test -r "$CONFIG_DIR/sources" 2>/dev/null; then
    echo "  readable by $SVC_USER: ok ($(as_svc find "$CONFIG_DIR/sources" -name '*.yaml' | wc -l) source files)"
else
    warn "$CONFIG_DIR is not readable by the $SVC_USER user."
    if ! findmnt -n "$NAS_MOUNT" >/dev/null 2>&1 && ! grep -qs " $NAS_MOUNT " /etc/fstab; then
        cat <<EOF

    The NAS share isn't mounted at $NAS_MOUNT. To mount it (needs the cifs-utils package):

    1. Credentials file, readable by root only:
         sudo install -m 600 /dev/null $ETC_DIR/nas.cred
         sudo nano $ETC_DIR/nas.cred        # two lines: username=...  password=...

    2. Add this line to /etc/fstab (replace NAS with the NAS hostname or IP):
         //NAS/Data  $NAS_MOUNT  cifs  credentials=$ETC_DIR/nas.cred,uid=$SVC_USER,gid=$SVC_USER,file_mode=0664,dir_mode=0775,vers=3.0,_netdev,nofail,x-systemd.automount  0  0

    3. sudo mkdir -p $NAS_MOUNT && sudo systemctl daemon-reload && sudo mount $NAS_MOUNT

    Then re-run this script. (nofail + automount: boot never waits on the NAS, and Readwell
    falls back to its cached config copy if the share is down.)
EOF
    else
        warn "$NAS_MOUNT is configured but $CONFIG_DIR isn't readable by $SVC_USER — check the mount's uid/gid options."
    fi
    if [[ ! -d $DATA_DIR/config-cache ]]; then
        warn "No cached config either: the services will start but stay idle until the config is readable."
    fi
fi
if ! as_svc test -w "$(dirname "$BACKUP_DIR")" 2>/dev/null; then
    warn "$(dirname "$BACKUP_DIR") is not writable by $SVC_USER; nightly backups will fail until it is."
else
    as_svc mkdir -p "$BACKUP_DIR"
fi

# ------------------------------------------------------------------ database
step "Database"
as_svc env READWELL_DATA_DIR="$DATA_DIR" "$APP_DIR/.venv/bin/readwell" migrate | sed 's/^/  /'

# ------------------------------------------------------------------ CLI wrapper
step "CLI wrapper (/usr/local/bin/readwell)"
install -m 755 "$APP_DIR/deploy/readwell-cli.sh" /usr/local/bin/readwell
echo "  installed: run 'readwell <command>' from any account (asks for sudo)"

# ------------------------------------------------------------------ systemd
step "systemd units"
install -m 644 "$APP_DIR"/deploy/readwell-{web,worker,backup}.service "$APP_DIR"/deploy/readwell-backup.timer \
    /etc/systemd/system/
if [[ $PORT != 8080 ]]; then
    install -d /etc/systemd/system/readwell-web.service.d
    printf '[Service]\nExecStart=\nExecStart=%s/.venv/bin/readwell serve --port %s\n' "$APP_DIR" "$PORT" \
        >/etc/systemd/system/readwell-web.service.d/port.conf
fi
systemctl daemon-reload
systemctl enable --quiet readwell-web.service readwell-worker.service readwell-backup.timer
systemctl restart readwell-web.service readwell-worker.service
systemctl start readwell-backup.timer
echo "  enabled and (re)started: readwell-web, readwell-worker, readwell-backup.timer"

# ------------------------------------------------------------------ smoke test
step "Smoke test"
ok=""
for _ in $(seq 1 20); do
    if out=$(curl -fsS "http://127.0.0.1:$PORT/healthz" 2>/dev/null); then ok=1; break; fi
    sleep 1
done
if [[ -n $ok ]]; then
    echo "  $out"
else
    warn "web app did not answer on port $PORT. Logs: journalctl -u readwell-web -n 50"
    exit 1
fi

ip=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i=1;i<=NF;i++) if ($i=="src") print $(i+1)}')
cat <<EOF

Readwell is running.
  Status page:  http://${ip:-<this-host>}:$PORT/
  All items:    http://${ip:-<this-host>}:$PORT/feeds/all.atom
  Health:       http://${ip:-<this-host>}:$PORT/feeds/health.atom
  OPML import:  http://${ip:-<this-host>}:$PORT/opml

Next:
  readwell check               find every source's feed (nothing is stored)
  readwell status              one line per source
  journalctl -u readwell-worker -f
EOF
