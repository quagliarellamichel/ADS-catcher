#!/bin/sh
# Copia adsb-catcher.py in ~/.local/bin e crea il lanciatore sul desktop.
set -e

SORGENTE="$(cd "$(dirname "$0")" && pwd)"
BIN="$HOME/.local/bin"
mkdir -p "$BIN"
install -m 755 "$SORGENTE/adsb-catcher.py" "$BIN/adsb-catcher.py"
echo "installato: $BIN/adsb-catcher.py"

# la cartella del desktop cambia con la lingua del sistema
DESKTOP="$(xdg-user-dir DESKTOP 2>/dev/null || true)"
[ -d "$DESKTOP" ] || DESKTOP="$HOME/Desktop"
[ -d "$DESKTOP" ] || DESKTOP="$HOME/Scrivania"

if [ -d "$DESKTOP" ]; then
    sed "s|@BIN@|$BIN/adsb-catcher.py|" "$SORGENTE/ADS-catcher.desktop.in" \
        > "$DESKTOP/ADS-catcher.desktop"
    chmod 755 "$DESKTOP/ADS-catcher.desktop"
    echo "lanciatore: $DESKTOP/ADS-catcher.desktop"
else
    echo "cartella del desktop non trovata, lanciatore saltato"
fi

case ":$PATH:" in
    *":$BIN:"*) ;;
    *) echo "NOTA: $BIN non e' nel PATH; aggiungilo per lanciarlo da terminale" ;;
esac
