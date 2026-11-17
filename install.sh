#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
SYS_USER_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

chmod +x "$DIR/mousetool.py" "$DIR/probe.py"

mkdir -p "$APP_DIR" "$SYS_USER_DIR"

sed "s|/home/lucasdonald/Documents/GitHub/mousetool|$DIR|g" "$DIR/mousetool.desktop" > "$APP_DIR/mousetool.desktop"
sed "s|/home/lucasdonald/Documents/GitHub/mousetool|$DIR|g" "$DIR/mousetool.service" > "$SYS_USER_DIR/mousetool.service"

echo "Installed desktop entry -> $APP_DIR/mousetool.desktop"
echo "Installed systemd user unit -> $SYS_USER_DIR/mousetool.service"

if command -v systemctl >/dev/null 2>&1; then
    systemctl --user daemon-reload
    systemctl --user enable --now mousetool.service
    echo "Enabled and started mousetool.service (127.0.0.1:8766)"
fi

if [[ "${1:-}" == "--udev" ]]; then
    sudo cp "$DIR/60-mousetool.rules" /etc/udev/rules.d/
    sudo udevadm control --reload-rules
    sudo udevadm trigger
    echo "Installed udev rules -> /etc/udev/rules.d/60-mousetool.rules"
else
    echo "Tip: install udev rules if WebHID cannot open your mouse:"
    echo "  sudo cp $DIR/60-mousetool.rules /etc/udev/rules.d/ && sudo udevadm control --reload-rules && sudo udevadm trigger"
fi

if getent group maccel >/dev/null 2>&1 && ! id -nG "$USER" | grep -qw maccel; then
    echo "Tip: add yourself to the maccel group so the Acceleration tab can save changes:"
    echo "  sudo usermod -aG maccel $USER   (then log out and back in)"
fi
