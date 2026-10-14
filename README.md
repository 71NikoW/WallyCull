# WallyCull

WallyCull sorts your photos by **technical quality** and by **value as wall art** (for WallyFrame).
It finds blurry, shaky, badly exposed shots, duplicates and weak burst frames, and moves the rejects to a
`_rejected` folder. It never deletes anything on its own.

Everything runs on your PC, including the AI. Your photos are never uploaded.

| | |
|---|---|
| Platform | Windows 10/11 (64-bit) |
| Formats | RAW (Fuji `.RAF`, `.CR2/.CR3`, `.NEF`, `.ARW`, `.DNG`, ...), JPG, PNG, TIFF, WebP |
| AI | Uses an NVIDIA graphics card when one is present, otherwise the CPU |
| Disk space | about 1 GB for the program, about 5 GB for the AI |

## Install

1. Open [Releases](../../releases/latest) and download `WallyCull-Setup-<version>.exe`.
2. Run it. If Windows SmartScreen warns you, click **More info → Run anyway** (the setup is not code-signed).
3. The setup installs the AI as its last step (about 5 GB download, once). You can keep working while it runs.
4. Start **WallyCull** from the Start menu or the desktop shortcut.

No admin rights needed. The program goes to `%LOCALAPPDATA%\Programs\WallyCull`.

## Quick start

1. **Choose folder**, for example `E:\DCIM\100_FUJI`.
2. **Analyze.** Runs in the background with low priority. Changes no files.
3. **Rate** some photos: ← reject, → keep, ↑ favorite. After 40 ratings your personal model learns your taste.
4. **Open report** to see every photo with its category and the reason.
5. **Move rejects → Preview → Move to `_rejected` now.**
6. Changed your mind? **Restore** puts photos back (single photos, one run, or everything).
7. Only when you are sure: **Delete for good** sends `_rejected` to the Recycle Bin after you type `DELETE`.

The [user guide](docs/user-guide.md) explains every step, the categories and all keyboard shortcuts.

## Categories

| Category | Meaning |
|---|---|
| **Highlight** | High value and technically strong. Among the best images of the folder. |
| **Keep** | Worth keeping, possibly despite small technical flaws. |
| **Review** | Unclear: technically fine but low value, or borderline. You decide. |
| **Reject** | Failed shot, duplicate, or a clearly weaker frame of a burst. Moved by step 5. |

From each burst WallyCull keeps the best shot, plus up to two more if they are really good.

## Updates

WallyCull checks for updates at every start and every 6 hours and installs them silently, then restarts.
Your ratings, settings and AI stay in place.

The repository is private, so the app needs your GitHub sign-in once: click **Connect with GitHub** in the
Updates card. If that does not work, create a read-only token with the button below it and paste it in.
**Previous version** in the same card rolls back one release.

## Where your data lives

| What | Where |
|---|---|
| Program (replaced by every update) | `%LOCALAPPDATA%\Programs\WallyCull` |
| Settings, AI packages, models, references, log | `%LOCALAPPDATA%\WallyCull` |
| Measurements, ratings, notes, report of a photo folder | `<photo folder>\.wallycull` |
| Rejected photos | `<photo folder>\_rejected` (same subfolders as the originals) |

Deleting `<photo folder>\.wallycull` resets that folder completely; your photos are not affected.

## Troubleshooting

| Problem | Fix |
|---|---|
| The AI card says "Not set up yet" | Click **Set up AI**. Needs an internet connection for the one-time download. |
| Updates say "Could not check" | Check the internet connection, then **Check now**. If GitHub denied access, sign in again. |
| The window does not open | Look at `%LOCALAPPDATA%\WallyCull\app.log`. Install the [WebView2 runtime](https://developer.microsoft.com/microsoft-edge/webview2/) if it is missing. |
| A photo is rated wrong | In **Rate**, press **M**, mark the area and describe the mistake. Your notes are collected in `.wallycull\annotations.csv`. |
| Too strict or too lenient | Create `%LOCALAPPDATA%\WallyCull\settings.yaml` with `classification:` → `strictness: 0.3` (lenient) to `0.7` (strict). |

## For developers

See [docs/development.md](docs/development.md): running from source, tests, architecture and the release process.
