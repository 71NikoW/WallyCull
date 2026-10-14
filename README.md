# WallyCull

Sortiert Fotos (Fuji RAF + JPG) nach technischer Qualität und Wert als Wandbild für WallyFrame.
Verschiebt Fehlschüsse sicher nach `_aussortiert`, löscht nie direkt. Läuft komplett lokal unter Windows 11,
die KI auf der eigenen Grafikkarte.

- **Installieren:** `WallyCull-Setup-<Version>.exe` aus den [Releases](../../releases/latest) starten.
- **Updates:** Sobald `__version__` in `fotosort/__init__.py` auf `main` steigt, baut GitHub Actions
  (`.github/workflows/setup.yml`) ein neues Setup und veröffentlicht es als Release. Die App findet und
  installiert es selbst.
- **Entwickeln:** `pip install -r requirements.txt`, dann `python -m fotosort` (öffnet die App).

Alles Weitere steht in der [Anleitung](ANLEITUNG.md).
