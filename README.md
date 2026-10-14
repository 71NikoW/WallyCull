# WallyCull

Sortiert Fotos (Fuji RAF + JPG) nach technischer Qualität und Wert als Wandbild für WallyFrame.
Verschiebt Fehlschüsse sicher nach `_aussortiert`, löscht nie direkt. Läuft komplett lokal unter Windows 11.

- **Einrichten:** Repo nach `%LOCALAPPDATA%\Programs\WallyCull` klonen, dann `Einrichten.bat -MitKI`.
- **Starten:** Verknüpfung **WallyCull** auf dem Desktop.
- **Updates:** Die App holt neue Versionen selbst aus diesem Repo (beim Start und alle 6 Stunden).
  Eine neue Version entsteht, sobald `__version__` in `fotosort/__init__.py` auf `main` steigt.

Alles Weitere steht in der [Anleitung](ANLEITUNG.md).
