# WallyCull – Anleitung

Die App bewertet deine Fotos nach technischer Qualität und Wert, findet Duplikate und Serienbilder
und verschiebt eindeutige Fehlschüsse in einen Ordner `_aussortiert`. **Sie löscht nie direkt.**
Alles läuft lokal auf deinem PC, keine Fotos verlassen den Rechner.

## 1. Installation (einmalig)

1. Auf GitHub unter **Releases** (<https://github.com/71NikoW/foto-aussortierer/releases/latest>) die Datei
   **`WallyCull-Setup-<Version>.exe`** herunterladen und starten.
   Windows meldet beim ersten Mal „Der Computer wurde durch Windows geschützt“, weil das Setup nicht signiert ist:
   **Weitere Informationen → Trotzdem ausführen**.
2. Das Setup installiert WallyCull samt eigenem Python und richtet danach automatisch die KI ein
   (PyTorch für deine NVIDIA-Grafikkarte, CLIP und die Bewertungsmodelle, einmalig ca. 5 GB Download).
3. Fertig: **WallyCull** liegt auf dem Desktop und im Startmenü. Administratorrechte braucht es nicht.

**Updates kommen von selbst:** Beim Start und alle 6 Stunden schaut WallyCull nach einem neuen Setup,
lädt es, installiert es unsichtbar und startet neu. Weil dein Repo privat ist, verbindest du die App beim
ersten Start einmal mit GitHub (rechts unter „Updates“). Falls ein Update Probleme macht: **„Zurück zu …“**.

## 2. Benutzung

Doppelklick auf **WallyCull**. Es öffnet sich das Programmfenster:

1. **Ordner wählen** (oder Pfad einfügen). Zuletzt benutzte Ordner stehen darunter.
2. **Analysieren:** misst alle Bilder und teilt sie ein. **Verändert keine Dateien.** Läuft mit niedriger
   Priorität im Hintergrund, unten siehst du den Fortschritt. Du kannst das Fenster sogar schließen, die
   Analyse läuft zu Ende. Beim nächsten Mal wird nur gemessen, was neu ist.
3. **Bewerten:** wie bei Tinder, dein persönliches Modell lernt mit (siehe unten).
4. **Bericht öffnen:** alle Bilder nach Kategorie mit Begründung.
5. **Aussortieren → Vorschau:** zeigt jedes Bild, das verschoben würde. Erst der Knopf darunter verschiebt.
6. **Zurückholen:** alle, einen ganzen Lauf oder einzelne Bilder per Klick.
7. **Endgültig löschen:** schiebt `_aussortiert` in den **Windows-Papierkorb**, erst nach Eintippen von `LÖSCHEN`.

> Tipp: Erst mit einer **Kopie** eines kleinen Ordners testen.

## 3. Bewerten und Lernen (wie bei Tinder)

Nach dem Analysieren auf **Bewerten** klicken. Es erscheint Bild für Bild:

| Taste | Wischen | Bedeutung |
|---|---|---|
| **←** | nach links | weg |
| **→** | nach rechts | behalten |
| **↑** | nach oben | Favorit |
| **1–5** | – | Sterne (1–2 = weg, 3–4 = behalten, 5 = Favorit) |
| **Leertaste** | antippen | 100-%-Ansicht zum Prüfen der Schärfe |
| **Rücktaste** | – | letzte Bewertung rückgängig |
| **M** | – | Markieren: Bereich aufziehen und notieren, was die Analyse falsch gemacht hat |

- Ab **40 Bewertungen** (je mindestens 10 „weg“ und „behalten“) trainiert sich das Modell und danach alle 10 Bewertungen neu. Das dauert Millisekunden.
- Ab dann zeigt es dir zuerst die Bilder, bei denen es **am unsichersten** ist. So lernt es mit wenig Aufwand am meisten. Oben steht seine Trefferquote, beim Bild seine Prognose und warum.
- Deine Bewertung gilt für das Bild immer. Für alle anderen Bilder entscheidet ab dann das Modell: unter 15 % „behalten“ heißt Aussortiert, unter 45 % Prüfen.
- Auch jedes Bild, das du aus `_aussortiert` zurückholst, wird zum Trainingsbeispiel.
- **Markieren (M):** Art wählen, optional einen Bereich im Bild aufziehen und kurz beschreiben, was falsch war:
  - *Hier ist das eigentliche Motiv:* Die Schärfe wird sofort genau dort neu gemessen und gilt auch bei späteren Analysen.
  - *Etwas im Weg / störend:* fließt als Merkmal ins Lernmodell ein.
  - *Schärfe / Verwacklung / Belichtung / Serie falsch* und *Sonstiges:* werden gesammelt. Alle Anmerkungen landen samt Messwerten in `.fotosort\anmerkungen.csv`. Die Datei kannst du mir schicken, dann verbessere ich gezielt die Analyse.
- **„Fertig & einteilen“** trainiert, teilt alle Bilder neu ein und aktualisiert den Bericht. Mit **„← Übersicht“** geht es zurück, dabei wird ebenfalls gespeichert und neu eingeteilt.

## 3b. KI-Modelle (Phase 2)

Das Setup richtet die KI automatisch ein (falls das einmal scheitert: rechts in der App **„KI einrichten“**). Ab dann läuft alles offline, deine Fotos verlassen den PC nie.

- **CLIP (ViT-L/14):** erkennt den Inhalt (Landschaft, Menschen, Tier, Architektur … oder Screenshot, Beleg, versehentliches Foto) und schätzt die Wandbild-Eignung.
- **LAION-Ästhetik:** Note 1–10, gelernt aus Hunderttausenden online bewerteten Fotos.
- **MUSIQ und NIMA** (optional, über pyiqa): technische Qualität und Ästhetik, trainiert auf Fotowettbewerben.
- **Vorbilder:** Lege 20–100 Bilder, die für dich gute Wandbilder sind, in den Vorbilder-Ordner (in der App rechts: **„Vorbilder-Ordner öffnen“**). Jedes Foto bekommt dann einen Wert dafür, wie nah es an diesem Stil liegt.

Daraus entsteht ein **Wert-Score**. Ab jetzt gilt die Matrix: Nur Bilder, die technisch schwach **und** wenig wert sind, werden aussortiert. Dein persönliches Modell (Bewerten) nutzt alle KI-Werte und die CLIP-Bildmerkmale mit. Es lernt also auch Motive und Bildstil.

Ohne KI läuft alles wie bisher, nur ohne Wert-Score.

## 4. Die vier Kategorien

| Kategorie | Bedeutung in Phase 1 |
|---|---|
| **Highlight** | Gehört zu den technisch besten 15 % des Ordners, ohne deutlichen Mangel |
| **Behalten** | Technisch in Ordnung, kein deutlicher Mangel |
| **Prüfen** | Deutlicher Mangel (z. B. verwackelt, zu weich), fast gleich gute Serienbilder oder nicht lesbare Dateien: du entscheidest |
| **Aussortiert** | Duplikate, schlechtere Serienbilder und eindeutige Fehlschüsse (schwarz, weiß, extrem unscharf) |

Weil es in Phase 1 noch keinen **Wert-Score** gibt, wird ein unscharfes, aber vielleicht wertvolles Bild
nie aussortiert, sondern landet in „Prüfen“. Ab Phase 2/3 entscheidet die Matrix aus Qualität **und** Wert.

## 5. Was gemessen wird

- **Schärfe auf dem Motiv:** auf dem Gesicht oder dem auffälligsten Bildbereich. Ein unscharfer Hintergrund (Bokeh) ist kein Fehler.
- **Detailschärfe:** Kantenbreite an den schärfsten Stellen des Motivs, gemessen bei 4000 px Bildbreite (ca. 50 cm Druck). Entscheidend, ob ein Bild auch groß an der Wand knackscharf wirkt.
- **Verwacklung:** Sind die Kanten in einer Richtung deutlich breiter als quer dazu, ist das Bild verwackelt.
- **Über-/Unterbelichtung:** Anteil ausgefressener bzw. abgesoffener Pixel. Nachtaufnahmen werden erkannt und nicht bestraft.
- **Rauschen** in glatten Flächen, **Verwacklung** (gerichtete Unschärfe).
- **Wandbild-Größe:** maximale Druckgröße bei 200 dpi aus der Original-Auflösung.
- **Duplikate/Serien:** Wahrnehmungs-Hash plus Aufnahmezeit. Von echten Duplikaten bleibt eines. Aus Serien bleiben bis zu 3 wirklich gute Bilder (Einstellung `serie_behalten`), fast gleich gute landen in „Prüfen“.

## 6. Fuji-RAW

- Analysiert wird das in jeder RAF-Datei eingebettete Kamera-JPG. Bei der X-T5 ist es 4416 × 2944 Pixel groß und reicht damit auch für die Detailschärfe. Ein Durchlauf über rund 2.700 Bilder dauert nur wenige Minuten.
- Mit `raw_entwickeln: true` in der `config.yaml` wird stattdessen jedes RAW vollständig entwickelt (deutlich langsamer).
- Liegen `DSCF1234.RAF` und `DSCF1234.JPG` (und z. B. `.xmp`) nebeneinander, zählen sie als **ein** Bild und werden immer **zusammen** verschoben.

## 7. Einstellungen anpassen

Alle Grenzwerte stehen kommentiert in **`config.yaml`** im Programmordner. Ändere sie aber nicht dort
(Updates überschreiben die Datei), sondern lege **`%LOCALAPPDATA%\WallyCull\eigene-einstellungen.yaml`** an und schreib nur
die geänderten Werte hinein, z. B.:

```yaml
einteilung:
  strenge: 0.6
duplikate:
  serie_behalten: 4
```

Die wichtigsten:

- `einteilung → strenge`: 0.0 nachsichtig … 1.0 streng.
- `qualitaet → schaerfe_unscharf / schaerfe_scharf`: Falls zu viele scharfe Bilder als „unscharf“ gelten, beide Werte senken.
- `duplikate → serie_behalten`: wie viele wirklich gute Bilder pro Serie behalten werden (Standard 3).
- `allgemein → volle_aufloesung`: `false` macht die Analyse viel schneller, verzichtet aber auf die 100-%-Schärfe.

Nach einer Änderung einfach wieder **Analysieren**: Gemessen wird nur, was neu ist, die Einteilung wird neu berechnet.

## 8. Wo liegt was?

- `<Foto-Ordner>\.fotosort\fotosort.db` – Datenbank mit allen Messwerten, deinen Bewertungen und jeder Verschiebung.
- `<Foto-Ordner>\.fotosort\modell.json` – dein trainiertes Modell.
- `<Foto-Ordner>\.fotosort\bericht.html` – der Bericht (Klick auf ein Bild zeigt alle Messwerte und Begründungen).
- `<Foto-Ordner>\_aussortiert\` – aussortierte Bilder mit unveränderter Ordnerstruktur.
- `%LOCALAPPDATA%\Programs\WallyCull` – das Programm (wird bei Updates ersetzt).
- `%LOCALAPPDATA%\WallyCull` – alles von dir: eigene Einstellungen, zuletzt benutzte Ordner, `app.log` (Fehlerprotokoll),
  KI-Pakete, Modelle und Vorbilder. Bleibt bei Updates und auch beim Deinstallieren erhalten.

## 9. Für Fortgeschrittene: Befehlszeile

Alles, was die App kann, geht auch ohne Fenster (im Programmordner ausführen):

```
python\python.exe -m fotosort analysieren "D:\Fotos" [--neu] [--kein-browser] [--ohne-ki]
python\python.exe -m fotosort einteilen "D:\Fotos"
python\python.exe -m fotosort aussortieren "D:\Fotos" [--ausfuehren]
python\python.exe -m fotosort zurueckholen "D:\Fotos" [--datei DSCF1234] [--lauf 2026-10-14_21-00-19] [--alle]
python\python.exe -m fotosort loeschen "D:\Fotos"
python\python.exe -m fotosort bewerten "D:\Fotos" [--port 8765]
python\python.exe -m fotosort trainieren "D:\Fotos"
python\python.exe -m fotosort fehlerliste "D:\Fotos"
python\python.exe -m fotosort bericht "D:\Fotos"
python\python.exe -m fotosort ki-installieren
```

## Nächste Phasen

3. Gesichtserkennung, Personen-Clustering, geschlossene Augen, Datum/GPS/Ereignisse.
4. Strenge-Schieberegler und Prüfen-Ansicht direkt in der App.
6. Optional: lokales Vision-Sprachmodell (Ollama) als Wert-Gutachter.
