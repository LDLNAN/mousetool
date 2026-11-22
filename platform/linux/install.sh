#!/usr/bin/env bash
set -euo pipefail

# Usage: ./install.sh [--udev] [--mouseaccel-perms]
#   --udev              install 60-mousetool.rules (hidraw access for WebHID)
#   --mouseaccel-perms  create the mouseaccel group and install 99-mouseaccel.rules + its udev helper, the
#                       mouseaccel counterpart of what the maccel package sets up for the maccel group

LINUX_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
DIR="$(cd -- "$LINUX_DIR/../.." && pwd)"
WANT_UDEV=0
WANT_MOUSEACCEL=0
for arg in "$@"; do
    case "$arg" in
        --udev) WANT_UDEV=1 ;;
        --mouseaccel-perms) WANT_MOUSEACCEL=1 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done
APP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
SYS_USER_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

chmod +x "$DIR/mousetool.py" "$DIR/tools/probe.py" "$LINUX_DIR/mouseaccel_param_ownership_and_resets"

mkdir -p "$APP_DIR" "$SYS_USER_DIR"

sed "s|@INSTALL_DIR@|$DIR|g" "$LINUX_DIR/mousetool.desktop" > "$APP_DIR/mousetool.desktop"
sed "s|@INSTALL_DIR@|$DIR|g" "$LINUX_DIR/mousetool.service" > "$SYS_USER_DIR/mousetool.service"

echo "Installed desktop entry -> $APP_DIR/mousetool.desktop"
echo "Installed systemd user unit -> $SYS_USER_DIR/mousetool.service"

if command -v systemctl >/dev/null 2>&1; then
    systemctl --user daemon-reload
    systemctl --user enable --now mousetool.service
    echo "Enabled and started mousetool.service (127.0.0.1:8766)"
fi

if [[ $WANT_UDEV == 1 ]]; then
    sudo cp "$LINUX_DIR/60-mousetool.rules" /etc/udev/rules.d/
    sudo udevadm control --reload-rules
    sudo udevadm trigger
    echo "Installed udev rules -> /etc/udev/rules.d/60-mousetool.rules"
else
    echo "Tip: install udev rules if WebHID cannot open your mouse:"
    echo "  sudo cp $LINUX_DIR/60-mousetool.rules /etc/udev/rules.d/ && sudo udevadm control --reload-rules && sudo udevadm trigger"
fi

# Kernel acceleration permissions. mouseaccel (preferred) mirrors the maccel package's setup: a group that owns
# /sys/module/<module>/parameters/*, /dev/<module> and /var/opt/<module>/resets.
if [[ $WANT_MOUSEACCEL == 1 ]]; then
    sudo groupadd -f mouseaccel
    sudo install -Dm755 "$LINUX_DIR/mouseaccel_param_ownership_and_resets" /usr/lib/udev/mouseaccel_param_ownership_and_resets
    sudo install -Dm644 "$LINUX_DIR/99-mouseaccel.rules" /etc/udev/rules.d/99-mouseaccel.rules
    sudo udevadm control --reload-rules
    echo "Installed mouseaccel udev rules -> /etc/udev/rules.d/99-mouseaccel.rules"
    echo "They apply the next time the mouseaccel module loads."
else
    echo "Tip: for mouseaccel, install its group and udev rules with: ./install.sh --mouseaccel-perms"
fi

for group in mouseaccel maccel; do
    if getent group "$group" >/dev/null 2>&1 && ! id -nG "$USER" | grep -qw "$group"; then
        label=$group
        [[ $group == maccel ]] && label="maccel (legacy)"
        echo "Tip: add yourself to the $group group so the Acceleration tab can save $label changes:"
        echo "  sudo usermod -aG $group $USER   (then log out and back in)"
    fi
done
