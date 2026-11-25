# 🧹 Mac-Aufräumer

Ein kleines Werkzeug, das zeigt, **wofür der Speicher auf deinem Mac draufgeht**, und beim Aufräumen hilft.

**🔒 Alles bleibt auf deinem Mac:** Das Programm baut keine Internetverbindung auf und lädt nichts hoch.
Die Oberfläche öffnet sich im Browser, aber der eingebaute Mini-Server ist nur von deinem eigenen Mac aus
erreichbar (`127.0.0.1`) und durch ein zufälliges Zugangs-Token geschützt. Es werden keine Zusatzpakete
installiert. Es läuft nur Python mit seiner Standardbibliothek und Programme, die in macOS eingebaut sind (`sips`, `mdls`, `ps` …).

## Was es kann

| Reiter | Inhalt |
|---|---|
| **Übersicht** | Freier Speicher und Aufteilung nach Kategorien (Bilder, Videos, Musik, Dokumente, Installer, Programme, App-Daten, Papierkorb …), dazu die größten Ordner und Programme sowie das, was sich schnell freimachen lässt |
| **Größte Dateien** | Die 600 größten Dateien, filterbar nach Kategorie, Name, Alter („älter als 2 Jahre“) und Größe |
| **Ordner** | Durch die Ordner klicken und sehen, welcher Unterordner wie viel belegt |
| **Programme** | Alle Apps nach Größe, **inklusive ihrer versteckten Daten** in ~/Library und wann sie zuletzt geöffnet wurden |
| **Duplikate** | Byte-genau identische Dateien (per Prüfsumme verglichen). Ein Klick wählt alle Kopien aus und lässt das Original stehen |
| **Ähnliche Bilder** | Optisch gleiche Fotos, auch in anderer Größe oder einem anderen Format (mit Vorschaubildern) |
| **Aufräum-Tipps** | Caches, Downloads, alte Installer, iPhone-Backups, Mail-Anhänge, Xcode-, Homebrew-, npm- und Docker-Daten, node_modules, Time-Machine-Schnappschüsse, jeweils mit Erklärung |
| **Leistung** | Arbeitsspeicher, Speicherdruck, Swap, Laufzeit seit dem letzten Neustart, welche Apps am meisten RAM/CPU brauchen, Hintergrund- und Startobjekte |

**Löschen ist sicher:** Ausgewähltes wird nur **in den Papierkorb** gelegt. Im Finder holst du es mit
„Zurücklegen“ wieder heraus. Wichtige Ordner (Benutzerordner, Dokumente, Library usw.) kann das Programm
gar nicht löschen.

## Starten

1. Den Ordner `mac-aufraeumer` auf den Mac kopieren (z. B. auf GitHub über **Code → Download ZIP**, entpacken).
2. Doppelklick auf **`Mac-Aufräumer starten.command`**.
   * Meldet macOS „Apple konnte nicht überprüfen, ob … frei von Schadsoftware ist“, klickst du auf **Fertig**
     (nicht auf Papierkorb). Dann gehst du zu **Systemeinstellungen → Datenschutz & Sicherheit**, scrollst nach
     unten zu „Sicherheit“ und klickst bei „Mac-Aufräumer starten.command“ auf **Dennoch öffnen**. Das ist nur
     einmal nötig.
   * Oder du umgehst die Meldung, indem du das Tool im Terminal startest (siehe unten).
   * Fehlt Python, bietet macOS automatisch die Installation der „Command Line Tools“ an. Einmal bestätigen
     und danach erneut starten.
3. Der Browser öffnet sich mit der Übersicht. Klicke auf **„Scan starten“**.
4. Zum Beenden schließt du das Terminal-Fenster.

Alternativ im Terminal:

```bash
cd ~/Downloads/mac-aufraeumer      # bzw. dort, wo der Ordner liegt
python3 mac_aufraeumer.py
```

### Berechtigungen

* Beim ersten Scan fragt macOS eventuell, ob das **Terminal** auf Schreibtisch, Dokumente, Downloads oder Daten
  anderer Apps zugreifen darf. Bitte **erlauben**, sonst fehlen diese Ordner in der Auswertung.
* Für einen **vollständigen** Scan (inkl. Mail, Nachrichten, Safari …):
  Systemeinstellungen → Datenschutz & Sicherheit → **Festplattenvollzugriff** → *Terminal* einschalten.
* Beim ersten Löschen fragt macOS, ob das Terminal den **Finder steuern** darf. Bitte erlauben, denn nur so landen
  die Dateien im Papierkorb mit der Funktion „Zurücklegen“.

## Gut zu wissen

* **iCloud-Dateien**, die nur in der Cloud liegen, werden nicht heruntergeladen und belegen auch keinen lokalen Platz.
  Sie werden deshalb bei Duplikaten ignoriert.
* **Fotos-Mediathek:** Wird als ein Block angezeigt. Löschen innerhalb der Mediathek bitte nur über die Fotos-App;
  dort gibt es das Album **„Duplikate“**.
* **Duplikate in ~/Library** sind standardmäßig ausgeblendet, weil dort Programm-Daten liegen. Mit dem Haken
  „Duplikate auch in App-Daten“ kannst du sie einblenden. Dann aber vorsichtig sein.
* Die Größen sind der **tatsächlich belegte** Platz auf der Festplatte (wie im Finder unter „Informationen“, in 1000er-Einheiten).
* Der Scan dauert je nach Datenmenge ein paar Sekunden bis wenige Minuten. Die Suche nach ähnlichen Bildern dauert
  bei vielen Fotos länger und lässt sich oben abschalten.
