#!/usr/bin/env bash
# Installe le relevé de santé du serveur (SMART, températures, services) pour DoktorTV Bot.
# Usage :  sudo ./host/install-health.sh /chemin/vers/DoktorTV-Bot/data
#          sudo ./host/install-health.sh --uninstall
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "À lancer avec sudo (root)."; exit 1; }
UNITS=/etc/systemd/system

if [ "${1:-}" = "--uninstall" ]; then
  systemctl disable --now doktortv-health.timer doktortv-smart-test.timer 2>/dev/null || true
  rm -f $UNITS/doktortv-health.{service,timer} $UNITS/doktortv-smart-test.{service,timer} /usr/local/bin/doktortv-health.py
  systemctl daemon-reload
  echo "Relevé de santé désinstallé."; exit 0
fi

DATA_DIR="$(realpath "${1:?Indique le dossier data du bot, ex. /opt/doktortv-bot/data}")"
command -v smartctl >/dev/null || { echo "smartctl manquant : apt install smartmontools"; exit 1; }
command -v python3  >/dev/null || { echo "python3 manquant : apt install python3"; exit 1; }
mkdir -p "$DATA_DIR"
install -m 755 "$(dirname "$0")/doktortv-health.py" /usr/local/bin/doktortv-health.py

cat > $UNITS/doktortv-health.service <<UNIT
[Unit]
Description=Relevé santé serveur pour DoktorTV Bot (SMART, températures, services)

[Service]
Type=oneshot
Environment="HEALTH_OUT=$DATA_DIR/health.json"
ExecStart=/usr/bin/python3 /usr/local/bin/doktortv-health.py
UNIT

cat > $UNITS/doktortv-health.timer <<UNIT
[Unit]
Description=Relevé santé toutes les 10 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=10min

[Install]
WantedBy=timers.target
UNIT

cat > $UNITS/doktortv-smart-test.service <<UNIT
[Unit]
Description=Test SMART court hebdomadaire (DoktorTV Bot)

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /usr/local/bin/doktortv-health.py --selftest
UNIT

cat > $UNITS/doktortv-smart-test.timer <<UNIT
[Unit]
Description=Test SMART court chaque dimanche à 4h30

[Timer]
OnCalendar=Sun *-*-* 04:30:00
Persistent=true

[Install]
WantedBy=timers.target
UNIT

systemctl daemon-reload
systemctl enable --now doktortv-health.timer doktortv-smart-test.timer
systemctl start doktortv-health.service
echo "OK : relevé écrit dans $DATA_DIR/health.json"
systemctl list-timers --no-pager | grep doktortv
