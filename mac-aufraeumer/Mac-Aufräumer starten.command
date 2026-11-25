#!/bin/bash
# Doppelklick startet den Mac-Aufräumer (läuft komplett lokal).
cd "$(dirname "$0")" || exit 1
if ! /usr/bin/python3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)" 2>/dev/null; then
  echo "Python 3 fehlt. macOS bietet gleich an, die 'Command Line Tools' zu installieren – bitte bestätigen"
  echo "und danach diese Datei noch einmal öffnen."
  /usr/bin/python3 --version
  read -r -p "Enter drücken zum Schließen …"
  exit 1
fi
exec /usr/bin/python3 mac_aufraeumer.py
