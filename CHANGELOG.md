# Changelog

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
