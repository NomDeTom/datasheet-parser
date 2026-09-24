#!/bin/sh
# Install the AutoNotes ingest units for the current user.
#   systemd/install.sh            install or update, enable, start
#   systemd/install.sh --remove   stop, disable, remove
# Needs ~/.config/autonotes/ingest.env (copy ingest.env.example). Requires systemd --user
# (on WSL: [boot] systemd=true in /etc/wsl.conf; `loginctl enable-linger` to run without a login).
set -eu
here=$(cd "$(dirname "$0")" && pwd)
parser=$(dirname "$here")
units="$HOME/.config/systemd/user"
env="$HOME/.config/autonotes/ingest.env"
names="autonotes-ingest.service autonotes-ingest.path autonotes-ingest.timer autonotes-nightly.service autonotes-nightly.timer"

if [ "${1:-}" = "--remove" ]; then
    systemctl --user disable --now autonotes-ingest.path autonotes-ingest.timer autonotes-nightly.timer 2>/dev/null || true
    for n in $names; do rm -f "$units/$n"; done
    systemctl --user daemon-reload
    echo "removed"; exit 0
fi

[ -f "$env" ] || { mkdir -p "$(dirname "$env")"; cp "$here/ingest.env.example" "$env";
                   echo "created $env — edit it, then run this again"; exit 1; }
vault=$(sed -n 's/^AUTONOTES_VAULT=//p' "$env")
[ -d "$vault/Library" ] || { echo "AUTONOTES_VAULT=$vault has no Library/ (run migrate_layout.py first)"; exit 1; }
inbox="$vault/Import files"
mkdir -p "$inbox" "$units"
for n in $names; do
    # PathChanged= takes the rest of the line as the path: spaces are literal (a \x20 escape
    # would be taken literally too, and the watch would silently never fire)
    sed -e "s|@PARSER_DIR@|$parser|g" -e "s|@INBOX@|$inbox|g" "$here/$n" > "$units/$n"
done
systemctl --user daemon-reload
systemctl --user enable --now autonotes-ingest.path autonotes-ingest.timer autonotes-nightly.timer
systemctl --user list-units 'autonotes-*' --all --no-pager
