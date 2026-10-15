# Changelog

## 0.10.0 - 2026-10-15

- **Understands race track photos.** The AI now asks motorsport questions about every photo, based on
  advice from motorsport photographers. Is a race car in the frame at all? Is it a good pan, with a sharp
  car and a streaked background? Are the wheels spinning? Is there room in the direction of travel? Is
  there action, such as sparks, smoke or a kerb strike? Is the background clean, and is the light good?
  Together with the shutter speed these become training features, so your model learns which of them
  matter to you. The report names them as reasons, e.g. "No race car in the frame".
- New content type "Motorsport" (counts as valuable).
- The AI scores are computed once more for all photos (fast, in the background).

## 0.9.2 - 2026-10-15

- Your ratings, notes and decisions are backed up on the PC (`%LOCALAPPDATA%\WallyCull\backups`) after
  every change. When a folder's database has to be started again, the next analysis takes them back.
- Warning when the photo folder is on a memory card or USB stick: copy it to the PC and work on the copy.

## 0.9.1 - 2026-10-15

- Fixed: a damaged WallyCull database in a photo folder (e.g. after the memory card was pulled out while
  writing) stopped the analysis with "file is not a database". It is now kept aside as
  `.wallycull\wallycull.db.damaged-<date>` and a fresh one is started. Your photos are not touched.

## 0.9.0 - 2026-10-15

- **Faster analysis.** Large camera JPGs and RAW previews are decoded about 3x faster: the full-resolution
  brightness used for the sharpness check is read straight from the JPEG, colors only at reduced size.
  The measurements stay the same, so photos analyzed before are not measured again.
- The AI models load in the background while the classic analysis runs (saves about 12 s per run), and the
  overnight run loads them only once for all folders.
- The AI prepares the next photos while the graphics card works, and NIMA scores several photos at once
  (almost 3x faster, same values).
- Duplicate fingerprints are computed from the preview (much faster, same result on real photos).

## 0.8.0 - 2026-10-15

- New: **Overnight training.** Start it in the evening; WallyCull analyzes every folder you used, sets up
  the AI if needed, trains your model thoroughly and sorts everything again. The PC stays awake meanwhile,
  and the result is shown in the app the next morning.
- Star ratings you gave in the camera (Fuji in-camera rating) or in Lightroom and other programs (including
  Lightroom's "rejected" flag) now count as training data. Your swipes in WallyCull always win.
- Smarter learning: the training tries several model sizes and keeps the one that predicts your verdicts
  best, and it spreads your verdicts to visually similar photos (label spreading on the CLIP features).
  Your reference images take part as "keep" examples.

## 0.7.0 - 2026-10-15

- Everything is in English now: app, rating screen, report, setup, settings and command line.
- New documentation: README, user guide and developer notes.
- Fixed the setup build (it ran out of memory while compressing).
- Rejected photos now go to `_rejected`; the confirmation word for the Recycle Bin is `DELETE`.
- Ratings, notes and moved photos from earlier versions are taken over automatically.
- Updates keep working if the GitHub repository is renamed.
- Automated tests for the whole workflow.

## 0.6.0 - 2026-10-14

- Real Windows setup with its own program window, AI installed automatically, silent self-updates.

## 0.5.0

- App window with all steps and self-update.

## 0.4.0 and earlier

- Analysis, classification, report, safe moving and restoring, Tinder-style rating with learning, AI value score.
