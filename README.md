# Roblox Portable – Downloader & Launcher

Portables Windows-Programm (Python 3.9+, nur Standardbibliothek + tkinter). Lädt den offiziellen
Roblox-Player von den offiziellen Roblox-Servern in `RobloxPortable/` neben dem Programm und startet ihn dort.
Es ändert keine Roblox-Dateien und berührt keinen Anti-Cheat – es ist nur Downloader und Launcher.

## Build (PyInstaller)

```bat
pip install pyinstaller
pyinstaller --onedir --windowed --name RobloxPortable roblox_portable.py
```

Ergebnis: `dist\RobloxPortable\RobloxPortable.exe` (ganzen Ordner `dist\RobloxPortable` kopieren/verschieben).

**Wichtig:** `--onefile` entpackt sich beim Start nach `%TEMP%\_MEIxxxx` und verletzt damit die Ordnerbegrenzung
(vor dem Programmcode, nicht änderbar). Für strikte Portabilität deshalb `--onedir` verwenden.
Kein `--uac-admin` verwenden (Standard ist „asInvoker“, keine UAC-Abfrage).
Ohne Build geht auch: `python roblox_portable.py`.

## Nutzung

1. Programm starten. Ist Roblox schon installiert, wird beim Start automatisch auf Updates geprüft; alte Versionen werden entfernt.
2. **Installieren/Aktualisieren**: lädt Manifest + Pakete, prüft MD5-Hashes, entpackt nach `RobloxPortable\Versions\<version>\`.
   Abgebrochene Downloads werden beim nächsten Klick fortgesetzt (`Downloads\…\*.part`). „Abbrechen“ ist jederzeit möglich.
3. **Starten**: startet `RobloxPlayerBeta.exe` als Kindprozess mit umgeleiteten `LOCALAPPDATA`, `APPDATA`, `TEMP`, `TMP`,
   `USERPROFILE` (alle unter `RobloxPortable\Data\`).
4. **Ordner öffnen**: öffnet `RobloxPortable\`. Logs: `Logs\launcher.log`, Leck-Berichte: `Logs\leckcheck-*.txt`.

### Anmeldung / Spiel starten (ohne Registry)

Das offizielle Browser-Login startet Spiele über das `roblox-player:`-Protokoll – das darf hier nicht in die Registry.
Deshalb gibt es zwei Wege:

* **App-Modus (Eingabefeld leer):** Start mit `--app`; Anmeldung und Spielauswahl dann im Roblox-Fenster selbst.
  *Bekannt aus anderen Launchern, hier nicht live getestet.*
* **roblox-player:-Link:** Ein Link im Eingabefeld wird in Kommandozeilenargumente (`--play -t … -j …`) übersetzt
  und direkt an die Player-EXE gegeben (nur `https://*.roblox.com` als Launcher-URL erlaubt, keine Shell).
  Das Anmeldeticket im Link gilt nur sehr kurz und einmalig.
* Ein reiner `roblox.com/games/…`-Link enthält kein Ticket → es wird der App-Modus gestartet.

Passwörter oder Cookies verarbeitet das Programm nie.

## Leck-Check

Vor dem Start, 20 s danach und nach dem Beenden werden verglichen: Roblox-Ordner im echten `%LOCALAPPDATA%`/`%APPDATA%`/`LocalLow`,
Dateien mit „roblox“/„rbx“ im Namen in Temp, Startmenü, Autostart, Desktop und Registry-Schlüssel (HKCU/HKLM `Software`,
`Software\Classes`, `Uninstall`, `Run`). Ergebnis steht im Protokollfeld und in `Logs\`.

## Bekannte Grenzen (offen dokumentiert)

* **Registry:** Roblox schreibt typischerweise nach `HKCU\Software\Roblox Corporation`. Das lässt sich ohne Hooks/Virtualisierung
  nicht umleiten; der Leck-Check meldet es dann. Die Daten-Ordner (Einstellungen, Logs, Cache) werden dagegen per Umgebungsvariablen umgeleitet,
  Roblox kann aber Windows-API-Known-Folders nutzen, die Umgebungsvariablen ignorieren.
* **Nicht getestet:** echter Download und Start (Test-Umgebung ohne Zugriff auf Roblox, ohne Windows). Getestet sind Download/Resume/Hash/Update/Zip-Schutz/
  Argument-Parser gegen ein lokales Fake-CDN (`python test_roblox_portable.py`). Die GUI wurde nicht ausgeführt (kein tkinter in der Testumgebung).
* Paket→Ordner-Zuordnung (`PACKAGE_DIRS`) entspricht dem bekannten Roblox-Layout; neue, unbekannte Pakete werden mit Warnung übersprungen
  und müssen dann ergänzt werden. Das Manifest-/API-Format ist aus dem Gedächtnis implementiert und muss beim ersten echten Lauf bestätigt werden.
* **Richtlinien:** Schul-/Firmenrechner können den Start trotzdem blockieren (AppLocker/Defender, Proxy/Firewall, gesperrte Domains
  `roblox.com`/`rbxcdn.com`, Ausführungsverbot in Benutzerordnern). Dann erscheinen verständliche Fehlermeldungen; umgehen kann und soll das Programm das nicht.
* Roblox-Anti-Cheat (Hyperion) kann das Starten aus nicht-standardmäßigen Ordnern oder unter eingeschränkten Konten ablehnen.
* Noch offen: Download parallelisieren, SHA/Signaturprüfung der EXE, ARM64-/Studio-Support, Single-Instance-Sperre.
