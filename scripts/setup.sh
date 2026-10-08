#!/usr/bin/env bash
# VM Manager first-run setup. Safe to re-run: every step checks before changing anything.
#
#   scripts/setup.sh                 # install deps, configure libvirt + firewall, install the service
#   scripts/setup.sh --listen 0.0.0.0 --port 8000
#   scripts/setup.sh --no-service    # everything except the systemd service (use ./run.sh)
#   scripts/setup.sh --no-boot       # start libvirt and the service now, but don't enable them at boot
#                                    # (e.g. a gaming PC: sudo systemctl start vm-manager when needed)
#   scripts/setup.sh --wg-ports 51820-51869   # UDP ports opened for lab WireGuard (WG_HOST_PORTS), none = skip
#   scripts/setup.sh --no-auth       # no login (AUTH_ENABLED=false in backend/.env): trusted single-user host only
#   scripts/setup.sh --auth          # turn the login back on (the default for new installs)
#
# Supported: Arch (and derivatives like CachyOS/Manjaro), Fedora, RHEL / AlmaLinux / Rocky / CentOS Stream,
# Debian / Ubuntu. Run it as the user who will use VM Manager; it calls sudo when needed.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LISTEN="127.0.0.1"
PORT="8000"
INSTALL_SERVICE=1
AT_BOOT=1
SERVICE_NAME="vm-manager"
WG_PORTS="51820-51869"
AUTH=""  # "" = keep the current setting (on by default), on, off

usage() { sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }
while [ $# -gt 0 ]; do
  case "$1" in
    --listen) LISTEN="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --no-service) INSTALL_SERVICE=0; shift ;;
    --no-boot) AT_BOOT=0; shift ;;
    --wg-ports) WG_PORTS="$2"; shift 2 ;;
    --no-auth) AUTH=off; shift ;;
    --auth) AUTH=on; shift ;;
    -h|--help) usage 0 ;;
    *) echo "Unknown option: $1" >&2; usage 1 ;;
  esac
done

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
step() { printf '\n\033[1;34m==>\033[0m \033[1m%s\033[0m\n' "$*"; }
ok()   { printf '    \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '    \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '\n\033[31mError:\033[0m %s\n' "$*" >&2; exit 1; }

if [ "$(id -u)" -eq 0 ]; then
  SUDO=""
  TARGET_USER="${SUDO_USER:-root}"
else
  command -v sudo >/dev/null || die "sudo is required (or run as root)"
  SUDO="sudo"
  TARGET_USER="$(id -un)"
fi
TARGET_GROUP="$(id -gn "$TARGET_USER")"
# enable_now <unit>: start a unit, and enable it at boot unless --no-boot
enable_now() { if [ "$AT_BOOT" = 1 ]; then $SUDO systemctl enable --now "$1"; else $SUDO systemctl start "$1"; fi; }
as_user() { if [ "$(id -un)" = "$TARGET_USER" ]; then "$@"; else $SUDO -u "$TARGET_USER" "$@"; fi; }
virsh_root() { $SUDO virsh -q -c qemu:///system "$@"; }

# ---------------------------------------------------------------------------
step "Detecting the distribution"
[ -r /etc/os-release ] || die "/etc/os-release not found"
# shellcheck disable=SC1091
. /etc/os-release
ids=" ${ID:-} ${ID_LIKE:-} "
case "$ids" in
  *" arch "*)                                   FAMILY=arch ;;
  *" fedora "*|*" rhel "*|*" centos "*)         FAMILY=rhel ;;
  *" debian "*|*" ubuntu "*)                    FAMILY=debian ;;
  *) die "Unsupported distribution '${PRETTY_NAME:-$ID}'. Install libvirt, QEMU and the libvirt Python bindings by hand, then re-run with the packages step removed." ;;
esac
ok "${PRETTY_NAME:-$ID} (family: $FAMILY), user: $TARGET_USER"

[ -e /dev/kvm ] && ok "/dev/kvm present" || warn "/dev/kvm missing: enable virtualization (VT-x/AMD-V) in the firmware, VMs will be very slow or fail"

# ---------------------------------------------------------------------------
step "Installing packages"
case "$FAMILY" in
  arch)
    pkgs=(libvirt dnsmasq edk2-ovmf libvirt-python python polkit)  # dnsmasq ships dhcp_release
    command -v qemu-system-x86_64 >/dev/null || pkgs+=(qemu-base)
    $SUDO pacman -S --needed --noconfirm "${pkgs[@]}"
    ;;
  rhel)
    $SUDO dnf install -y libvirt-daemon-kvm libvirt-daemon-config-network libvirt-client python3 python3-libvirt \
      dnsmasq-utils polkit
    ;;
  debian)
    $SUDO apt-get update -qq
    $SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
      libvirt-daemon-system libvirt-clients qemu-system-x86 qemu-utils ovmf dnsmasq-base python3 python3-venv python3-libvirt \
      dnsmasq-utils polkitd pkexec
    ;;
esac
ok "packages installed"

PYTHON=/usr/bin/python3
"$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 9))' || die "Python 3.9+ is required ($("$PYTHON" --version))"
"$PYTHON" -c 'import libvirt' 2>/dev/null || die "the libvirt Python bindings are not importable by $PYTHON"
ok "$("$PYTHON" --version) with libvirt bindings"

# ---------------------------------------------------------------------------
step "Starting libvirt"
if [ "$FAMILY" = rhel ]; then
  # Fedora/RHEL use per-driver ("modular") daemons
  for drv in qemu network nodedev nwfilter secret storage interface; do
    for sock in "virt${drv}d.socket" "virt${drv}d-ro.socket" "virt${drv}d-admin.socket"; do
      systemctl list-unit-files "$sock" >/dev/null 2>&1 && enable_now "$sock" >/dev/null 2>&1 || true
    done
  done
else
  enable_now libvirtd.service >/dev/null 2>&1
fi
for _ in $(seq 20); do virsh_root version >/dev/null 2>&1 && break; sleep 0.5; done
virsh_root version >/dev/null || die "cannot connect to qemu:///system"
virsh_root domcapabilities --virttype kvm >/dev/null 2>&1 && ok "libvirt is running with KVM" \
  || warn "libvirt runs but reports no KVM emulator; check QEMU installation and /dev/kvm"

# ---------------------------------------------------------------------------
step "Allowing $TARGET_USER to manage VMs (libvirt group)"
getent group libvirt >/dev/null || die "group 'libvirt' does not exist (libvirt packaging issue)"
if [ "$TARGET_USER" = root ] || id -nG "$TARGET_USER" | tr ' ' '\n' | grep -qx libvirt; then
  ok "$TARGET_USER already in group libvirt"
else
  $SUDO usermod -aG libvirt "$TARGET_USER"
  ok "added $TARGET_USER to group libvirt (log out and back in for virsh in your own shells; the service gets it now)"
fi

# ---------------------------------------------------------------------------
step "Privileged helper and polkit rules"
# - /usr/libexec/vm-manager/helper: root-owned, fixed command whitelist (dhcp-release), run via pkexec
# - polkit: group libvirt may run it, and start/stop the libvirt daemons + sockets (Start/Stop in the UI)
HELPER_DIR=/usr/libexec/vm-manager
$SUDO install -d -o root -g root -m 0755 "$HELPER_DIR"
$SUDO install -o root -g root -m 0755 "$APP_DIR/scripts/vm-manager-helper" "$HELPER_DIR/helper"
$SUDO install -D -o root -g root -m 0644 "$APP_DIR/scripts/polkit/org.vmmanager.helper.policy" \
  /usr/share/polkit-1/actions/org.vmmanager.helper.policy
$SUDO install -d -m 0755 /etc/polkit-1/rules.d 2>/dev/null || true
$SUDO install -o root -g root -m 0644 "$APP_DIR/scripts/polkit/50-vm-manager.rules" /etc/polkit-1/rules.d/50-vm-manager.rules
# SELinux: label the copies like their directories (bin_t, etc_t, usr_t)
if command -v restorecon >/dev/null 2>&1; then
  $SUDO restorecon -RF "$HELPER_DIR" /usr/share/polkit-1/actions/org.vmmanager.helper.policy /etc/polkit-1/rules.d/50-vm-manager.rules
fi
$SUDO sh -c 'command -v pkexec' >/dev/null 2>&1 || warn "pkexec not found: DHCP lease release will not work (install polkit / pkexec)"
$SUDO sh -c 'command -v dhcp_release' >/dev/null 2>&1 || warn "dhcp_release not found: DHCP lease release will not work"
# polkitd picks up rule changes by itself; restart it only if it isn't running
systemctl is-active --quiet polkit 2>/dev/null || $SUDO systemctl start polkit 2>/dev/null || true
ok "helper in $HELPER_DIR, polkit rule /etc/polkit-1/rules.d/50-vm-manager.rules"

# ---------------------------------------------------------------------------
step "Login (Linux accounts through PAM)"
# /etc/pam.d/vm-manager: the distro's normal password stack (pam_unix, SSSD…) + account checks.
# Group vm-manager: its members (and wheel / sudo, AUTH_ADMIN_GROUPS) may log in as admins.
PAM_FILE=/etc/pam.d/vm-manager
if [ -f "$PAM_FILE" ] && ! grep -q "Installed by VM Manager" "$PAM_FILE"; then
  ok "$PAM_FILE exists and is not ours: kept"
else
  if [ -f /etc/pam.d/system-auth ]; then      # EL, Fedora, Arch
    pam_stack=$'auth     include system-auth\naccount  include system-auth'
  elif [ -f /etc/pam.d/common-auth ]; then    # Debian, Ubuntu
    pam_stack=$'@include common-auth\n@include common-account'
  else
    pam_stack=$'auth     required pam_unix.so\naccount  required pam_unix.so'
  fi
  printf '#%%PAM-1.0\n# Installed by VM Manager (scripts/setup.sh): password check of the web UI login (docs/auth.md)\n%s\n' \
    "$pam_stack" | $SUDO tee "$PAM_FILE.new" >/dev/null
  $SUDO chmod 0644 "$PAM_FILE.new" && $SUDO mv -f "$PAM_FILE.new" "$PAM_FILE"
  if command -v restorecon >/dev/null 2>&1; then $SUDO restorecon "$PAM_FILE"; fi
  ok "PAM service $PAM_FILE"
fi
getent group vm-manager >/dev/null || { $SUDO groupadd --system vm-manager && ok "created group vm-manager"; }
ENV_FILE="$APP_DIR/backend/.env"
set_env() {  # set_env KEY VALUE: replace or append in backend/.env (kept by updates, not in git)
  if [ -f "$ENV_FILE" ] && grep -q "^$1=" "$ENV_FILE"; then as_user sed -i "s|^$1=.*|$1=$2|" "$ENV_FILE"
  else printf '%s=%s\n' "$1" "$2" | as_user tee -a "$ENV_FILE" >/dev/null; fi
}
case "$AUTH" in
  on)  set_env AUTH_ENABLED true ;;
  off) set_env AUTH_ENABLED false ;;
esac
if [ -f "$ENV_FILE" ] && grep -qiE '^AUTH_ENABLED=(false|0|no|off)' "$ENV_FILE"; then
  AUTH_STATE=off
  warn "login DISABLED (AUTH_ENABLED=false in $ENV_FILE): anyone reaching port $PORT manages this host's VMs"
  [ "$LISTEN" = 127.0.0.1 ] || warn "and the app listens on $LISTEN: use --auth unless this network is trusted"
else
  AUTH_STATE=on
  ok "login with Linux accounts: $TARGET_USER, and members of groups vm-manager, wheel, sudo"
fi

# ---------------------------------------------------------------------------
step "Default network"
if ! virsh_root net-info default >/dev/null 2>&1; then
  template=/usr/share/libvirt/networks/default.xml
  [ -r "$template" ] || die "network 'default' does not exist and $template is missing"
  virsh_root net-define "$template" >/dev/null
  ok "defined network 'default'"
fi
if ! virsh_root net-list --name | grep -qx default; then
  if ! err="$(virsh_root net-start default 2>&1)"; then
    case "$err" in
      *"already in use"*)
        # The default 192.168.122.0/24 collides with an existing network (LAN, VPN, or we run nested):
        # move it to the first free 192.168.X.0/24.
        used="$(ip -4 -o addr show; ip -4 route show)"
        free=""
        for x in $(seq 100 254); do
          echo "$used" | grep -q "192\.168\.$x\." || { free=$x; break; }
        done
        [ -n "$free" ] || die "network 'default': no free 192.168.X.0/24 subnet found"
        old_xml="$(virsh_root net-dumpxml --inactive default)"
        old_net="$(echo "$old_xml" | grep -o "address='192\.168\.[0-9]*\." | head -1 | cut -d"'" -f2)"
        [ -n "$old_net" ] || die "network 'default' is in conflict and is not a 192.168.x.0/24 network: fix it by hand ($err)"
        tmp="$(mktemp)"
        echo "$old_xml" | sed "s/${old_net//./\\.}/192.168.$free./g" > "$tmp"
        virsh_root net-define "$tmp" >/dev/null
        rm -f "$tmp"
        virsh_root net-start default >/dev/null
        warn "192.168.122.0/24 is already used on this host: network 'default' moved to 192.168.$free.0/24"
        ;;
      *) die "cannot start network 'default': $err" ;;
    esac
  fi
fi
virsh_root net-autostart default >/dev/null
ok "network 'default' active, autostart on ($(virsh_root net-dumpxml default | grep -o "address='[0-9.]*'" | head -1 | cut -d"'" -f2)/24)"

# ---------------------------------------------------------------------------
step "Firewall"
# (ufw / firewall-cmd live in /usr/sbin on some distros: not in a normal user's PATH)
if $SUDO sh -c 'command -v ufw' >/dev/null 2>&1 && $SUDO ufw status 2>/dev/null | grep -q "Status: active"; then
  # ufw's default policies drop DHCP/DNS from VMs and forwarded (NATed) traffic on libvirt bridges
  # (ufw skips rules that already exist)
  $SUDO ufw allow in on 'virbr+' to any port 67 proto udp comment 'libvirt DHCP' >/dev/null
  $SUDO ufw allow in on 'virbr+' to any port 53 comment 'libvirt DNS' >/dev/null
  $SUDO ufw route allow in on 'virbr+' comment 'libvirt NAT out' >/dev/null
  $SUDO ufw route allow out on 'virbr+' comment 'libvirt NAT in' >/dev/null
  ok "ufw: DHCP, DNS and forwarding allowed on libvirt bridges (virbr+)"
  if [ "$WG_PORTS" != none ]; then
    # lab groups' WireGuard (remote access): the app relays these UDP ports to the group routers
    $SUDO ufw allow "${WG_PORTS/-/:}/udp" comment 'vm-manager WireGuard (lab remote access)' >/dev/null
    ok "ufw: udp $WG_PORTS allowed (lab WireGuard)"
  fi
elif systemctl is-active --quiet firewalld 2>/dev/null; then
  if $SUDO firewall-cmd --get-zones | tr ' ' '\n' | grep -qx libvirt; then
    ok "firewalld: libvirt manages its bridges in the 'libvirt' zone, nothing to do"
  else
    warn "firewalld has no 'libvirt' zone: VMs may not get DHCP. Allow dhcp/dns on virbr* interfaces."
  fi
  if [ "$WG_PORTS" != none ]; then
    # default zone (the LAN interface's): runtime + permanent, no reload
    if ! $SUDO firewall-cmd --query-port="$WG_PORTS/udp" >/dev/null 2>&1; then
      $SUDO firewall-cmd --add-port="$WG_PORTS/udp" >/dev/null
    fi
    if ! $SUDO firewall-cmd --permanent --query-port="$WG_PORTS/udp" >/dev/null 2>&1; then
      $SUDO firewall-cmd --permanent --add-port="$WG_PORTS/udp" >/dev/null
    fi
    ok "firewalld: udp $WG_PORTS open in zone $($SUDO firewall-cmd --get-default-zone) (lab WireGuard)"
  fi
else
  ok "no ufw/firewalld active, nothing to do"
fi
if [ "$LISTEN" != 127.0.0.1 ] && [ "$LISTEN" != localhost ]; then
  warn "listening on $LISTEN: open port $PORT/tcp in your firewall yourself. There is no login, use a trusted network only."
fi

# ---------------------------------------------------------------------------
step "Python environment"
VENV="$APP_DIR/backend/venv"
if [ -x "$VENV/bin/python" ] && "$VENV/bin/python" -c 'import libvirt' 2>/dev/null; then
  ok "reusing $VENV"
else
  rm -rf "$VENV"
  # system site packages: use the distro's libvirt bindings (no compiler / headers needed)
  as_user "$PYTHON" -m venv --system-site-packages "$VENV"
  ok "created $VENV"
fi
grep -v '^libvirt-python' "$APP_DIR/backend/requirements.txt" > "$APP_DIR/backend/.requirements-pip.txt"
as_user "$VENV/bin/pip" install -q --disable-pip-version-check -r "$APP_DIR/backend/.requirements-pip.txt"
rm -f "$APP_DIR/backend/.requirements-pip.txt"
(cd "$APP_DIR/backend" && as_user "$VENV/bin/python" -c 'import app.main') || die "backend does not import, see the error above"
ok "backend dependencies installed"

# ---------------------------------------------------------------------------
step "Web UI"
if [ -f "$APP_DIR/frontend/build/index.html" ]; then
  ok "using the built UI in frontend/build"
elif command -v npm >/dev/null; then
  (cd "$APP_DIR/frontend" && as_user npm ci --legacy-peer-deps --no-audit --no-fund --loglevel=error && as_user npm run build --silent)
  ok "built the UI"
else
  die "frontend/build is missing and npm is not installed: install Node.js 20+, or use a release tarball (UI prebuilt)"
fi

# ---------------------------------------------------------------------------
if [ "$INSTALL_SERVICE" = 1 ]; then
  step "systemd service ($SERVICE_NAME)"
  unit="/etc/systemd/system/$SERVICE_NAME.service"
  $SUDO tee "$unit" >/dev/null <<EOF
[Unit]
Description=VM Manager (web UI and API for libvirt)
After=network-online.target libvirtd.service virtqemud.socket
Wants=network-online.target

[Service]
User=$TARGET_USER
Group=$TARGET_GROUP
SupplementaryGroups=libvirt
# Started through /bin/sh: with SELinux (RHEL/Fedora), systemd itself may not access files in a
# home directory, but the service process it starts may.
ExecStart=/bin/sh -c 'cd "$APP_DIR/backend" && exec "$VENV/bin/python" -m uvicorn app.main:app --host $LISTEN --port $PORT --timeout-graceful-shutdown 3'
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
  $SUDO systemctl daemon-reload
  if [ "$AT_BOOT" = 1 ]; then
    $SUDO systemctl enable "$SERVICE_NAME" >/dev/null 2>&1
  else
    $SUDO systemctl disable "$SERVICE_NAME" >/dev/null 2>&1 || true
  fi
  $SUDO systemctl restart "$SERVICE_NAME"
  host="$LISTEN"; [ "$host" = 0.0.0.0 ] && host=127.0.0.1
  healthy() { "$PYTHON" -c "import urllib.request; urllib.request.urlopen('http://$host:$PORT/health', timeout=2)" 2>/dev/null; }
  for _ in $(seq 40); do healthy && break; sleep 0.5; done
  if healthy; then
    [ "$AT_BOOT" = 1 ] && ok "running, enabled at boot" || ok "running (not enabled at boot: sudo systemctl start $SERVICE_NAME)"
  else
    $SUDO systemctl --no-pager status "$SERVICE_NAME" | tail -15 || true
    die "the service did not come up: journalctl -u $SERVICE_NAME"
  fi
fi

# ---------------------------------------------------------------------------
echo
bold "VM Manager is ready."
if [ "$INSTALL_SERVICE" = 1 ]; then
  echo "  Open http://$([ "$LISTEN" = 0.0.0.0 ] && hostname -f || echo "$LISTEN"):$PORT"
  echo "  Logs: journalctl -u $SERVICE_NAME -f      Stop: sudo systemctl stop $SERVICE_NAME"
else
  echo "  Start it with: HOST=$LISTEN PORT=$PORT ./run.sh"
fi
if [ "$AUTH_STATE" = on ]; then
  echo "  Log in with your Linux account ($TARGET_USER) and its password."
  echo "  Other people: sudo usermod -aG vm-manager <user>   (read-only access: AUTH_VIEWER_GROUPS in backend/.env)"
  echo "  API token for scripts / OpenTofu (or create one in the UI: user menu > API tokens):"
  echo "    (cd $APP_DIR/backend && venv/bin/python -m app.cli token create --user $TARGET_USER --name opentofu)"
else
  echo "  No login (AUTH_ENABLED=false): turn it on with scripts/setup.sh --auth"
fi
echo "  First steps: Storage > Cloud images > Download, then Virtual machines > Create VM."
