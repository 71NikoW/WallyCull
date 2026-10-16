# WallyCull user guide

## How WallyCull decides

WallyCull rates every photo on two axes:

- **Quality** (0 to 1): sharpness on the subject (faces count extra), fine detail at 100% view,
  camera shake, exposure (blown highlights, crushed shadows, with a night-shot exception), noise and
  resolution for large prints.
- **Value** (0 to 1, needs the AI): aesthetics (LAION model trained on rated photos), wall-art potential,
  image content (a screenshot or a receipt is worth less than a landscape), closeness to your reference
  images, and the NIMA aesthetics score.

The two together give the category:

| | technically good | technically poor |
|---|---|---|
| **high value** | Highlight | Keep |
| **low value** | Review | Reject |

Without AI data WallyCull only rejects clear failures and duplicates; anything borderline goes to *Review*.
Once you have rated enough photos, **your personal model** takes over (see "Rate").

Your own decision always wins: a photo you rated or restored keeps your verdict.

## Steps in the app

### 1. Analyze

Reads every photo in the folder and its subfolders. RAW and JPG files with the same name count as one
photo (for the X-T5 the embedded camera JPG is used, which is fast and shows your film simulation).
Measurements are cached: the next run only analyzes new or changed photos. Tick **measure everything
again** after an update that improves the analysis.

You can close the window while it runs. The analysis finishes in the background; opening WallyCull
again shows its progress.

### 2. Rate

| Key | Action |
|---|---|
| → or swipe right | Keep |
| ← or swipe left | Reject |
| ↑ or swipe up | Favorite |
| 1 to 5 | Stars (1-2 reject, 3-4 keep, 5 favorite) |
| Space or click | Zoom to 100% (move the mouse to pan) |
| Backspace or U | Undo |
| M | Mark: tell WallyCull what it got wrong |
| Ctrl+Enter / Esc | Save note / close the marking panel |

After 40 ratings (at least 10 keeps and 10 rejects) WallyCull trains a model of your taste and retrains
every 10 ratings. From then on it shows the photos it is least sure about first, so every rating teaches
it as much as possible. The header shows how often the model agrees with you.

**Mark** opens the photo large. Pick what went wrong, optionally drag a box, and add a short note:

- *This is the actual subject*: sharpness is measured again on exactly that area, right away.
- *Something in the way*, *Sharpness judged wrong*, *Camera shake detected wrongly*, *Exposure judged
  wrong*, *Burst/duplicate detected wrongly*, *Other*: collected in `.wallycull\annotations.csv`, so the
  analysis can be improved.

Click **← Overview** or **Done & classify** to save and classify all photos with the new model.

### Overnight training

**Start overnight run** (right column) is meant for the evening. For every photo folder you used in the app it

1. analyzes new photos and computes the AI scores (and sets up the AI first if it is missing),
2. reads the star ratings you gave in the camera or in Lightroom, Capture One, Bridge, ...:
   3-4 stars count as keep, 5 stars as favorite, Lightroom's "rejected" flag as reject (1-2 stars are ignored),
3. trains your model thoroughly: it tries several model sizes, keeps the one that predicts your verdicts best,
   and spreads your verdicts to visually similar photos, so one verdict on a burst or a motif helps with all
   similar shots; your reference images count as "keep" examples,
4. sorts everything again and writes the report.

The PC does not go to sleep while it runs (the screen may still turn off). The next morning the card shows
what happened per folder and how often the model now agrees with you.

What it cannot do: learn your taste from nothing. It needs at least 40 verdicts per folder (swipes, stars or
restored photos, at least 10 keep and 10 reject). Ten minutes of swiping in the morning, or stars you already
gave in the camera, make every following night much more useful.

### 3. Report

A grid of all photos, filtered by category and sortable by quality, name, date or burst. Click a photo
for every reason and every partial score.

### 4. Move rejects

**Preview** lists everything that would be moved, with the reason. **Move to _rejected now** moves the
photos together with their RAW, JPG and sidecar files (`.xmp`) into `_rejected`, keeping the subfolders.

### 5. Restore

Lists every moved photo, grouped by run. Restore a single photo, a whole run, or everything. A restored
photo counts as "keep" for your model.

### 6. Delete for good

Sends the contents of `_rejected` to the Windows Recycle Bin. Type `DELETE` to confirm. Even then the
photos can still be recovered from the Recycle Bin until you empty it.

## Memory cards and backups

Work on a copy on your PC, not directly on the memory card: WallyCull writes its data into the photo folder,
and a flaky card reader can damage files while writing.

When you choose a folder on a memory card, the app shows step **0. Import from memory card**. It copies
everything (photos, sidecars, videos) to `Pictures\WallyCull\<date>_<card folder>`, then reads the card and
the copy again, bypassing the Windows file cache, and compares them bit for bit. A wrong copy is copied again;
the card is only ever read. Afterwards the app switches to the copy. An interrupted import simply continues.
Only format the card when the import says everything is identical. On the command line:
`wallycull import E:\DCIM\100_FUJI` (optionally `--to D:\Photos\Race`).

Your ratings, notes and decisions are also copied to `%LOCALAPPDATA%\WallyCull\backups` after every change.
If the folder's database is ever damaged, WallyCull keeps the broken file as
`.wallycull\wallycull.db.damaged-<date>`, starts a new one, measures the photos again and takes your ratings
back from the backup.

## One model for all folders

Every folder learns from your ratings in all your other folders as well (stored as numbers, not photos, in
`%LOCALAPPDATA%\WallyCull\training`). A new folder from the next race day is therefore sorted by your taste
right after the analysis, before you rate a single photo there. To keep folders separate, set
`learning: shared: false` in your settings.

## Start numbers

After the AI step, WallyCull reads the start number on the car (locally, no internet; the small text
recognition is installed once, about 60 MB) and looks it up in the entry lists of DTM 2026 and Porsche Carrera
Cup Deutschland 2026. The report then shows e.g. "#91 Thomas Preining (Manthey Racing, DTM 2026)" and has a
car filter. Only numbers from an entry list count, so sponsor text is ignored. Numbers used in two series
show both drivers.

The entry lists are CSV files (`number,driver,team,car`) in the program's `wallycull\series` folder. Put a
corrected or additional list into `%LOCALAPPDATA%\WallyCull\series` (same file name replaces the built-in one).
`wallycull numbers <folder>` reads the numbers and saves `.wallycull\numbers.csv` with every photo per driver.

## References

Put 20 to 100 photos you consider great wall art (for example your best sellers) into the references
folder: **AI rating → Open references folder**. During the next analysis photos in a similar style gain
value. You can change the folder at any time.

## Settings

The defaults are in `config.yaml` in the program folder. Don't edit that file, updates replace it.
Instead create `%LOCALAPPDATA%\WallyCull\settings.yaml` with only the values you want to change, for example:

```yaml
classification:
  strictness: 0.6        # 0.0 lenient ... 1.0 strict
duplicates:
  burst_keep: 2          # keep at most 2 photos per burst
quality:
  wallart_min_megapixels: 12
```

Run **Analyze** again afterwards; only the classification is recomputed.

## Command line

The app covers everything, but each step also works from a terminal in the program folder:

```bat
python\python.exe -m wallycull analyze "E:\DCIM\100_FUJI"
python\python.exe -m wallycull reject "E:\DCIM\100_FUJI"            &rem dry run
python\python.exe -m wallycull reject "E:\DCIM\100_FUJI" --execute
python\python.exe -m wallycull restore "E:\DCIM\100_FUJI" --file DSCF1234
python\python.exe -m wallycull --help
```

## Upgrading from the German version (up to 0.6)

Nothing to do. On the first analysis WallyCull takes over your ratings, notes and moved photos from the
old `.fotosort` folder and measures every photo again. The old folder stays untouched; delete it once you
are happy. Photos in the old `_aussortiert` folder can still be restored from the app.
