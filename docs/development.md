# Development

## Run from source

Python 3.12 on Windows (Linux works for everything except the app window):

```bash
python -m venv .venv
.venv\Scripts\activate            # Linux: source .venv/bin/activate
pip install -r requirements.txt
pip install -r requirements-ai.txt torch torchvision   # optional, for the AI
python -m wallycull               # opens the app
python -m wallycull --help        # command line
```

`WALLYCULL_DATA=<folder>` moves the user data folder (handy for testing).
Updates are disabled when running from source (status "Developer version").

## Tests

```bash
pip install pytest
python -m pytest -q
```

The tests create synthetic photos and run the whole workflow (analyze, classify, move, restore, training,
migration from the German database). They run on every push (`.github/workflows/tests.yml`).

## Architecture

```
wallycull/
  __main__.py     python -m wallycull
  cli.py          commands (app, analyze, classify, reject, restore, purge, rate, train, ...)
  app.py          app window (pywebview/WebView2) + local HTTP server on 127.0.0.1, background tasks, updates
  rating.py       rating screen (swipe, marking) and its HTTP API, shared with app.py
  paths.py        program folder, user data folder, AI packages on sys.path
  config.py       config.yaml + the user's settings.yaml
  database.py     SQLite schema in <photo folder>/.wallycull/wallycull.db, migration from .fotosort
  loader.py       find files, group RAW/JPG/sidecars, load images and EXIF
  quality.py      technical measurements and the quality score
  analyze.py      parallel analysis with caching
  duplicates.py   duplicates and bursts (perceptual hashes + capture time)
  ai.py           CLIP, LAION aesthetics, pyiqa (MUSIQ/NIMA), references -> value score
  ai_setup.py     installs PyTorch/CLIP into the user folder (called by the setup)
  learning.py     personal model (logistic regression on measurements + CLIP features)
  classify.py     the category matrix and the human-readable reasons
  files.py        move to _rejected, restore, purge to the Recycle Bin
  report.py       HTML report and annotations CSV
  updater.py      GitHub releases: check, download, silent setup
installer/        Inno Setup script
.github/workflows setup.yml builds and publishes the setup, tests.yml runs the tests
```

Design rules:

- **Never delete directly.** Every move is recorded in the `moves` table and can be undone.
- Paths in the database are relative to the photo folder, so a changed drive letter breaks nothing.
- Heavy work runs as a separate process (`python -m wallycull <command>`); the app reads its output.
  Progress bars come from tqdm lines (`Title: 45%|...| 12/30`). Keep that format when adding steps.
- The local server only answers requests to `127.0.0.1`/`localhost`, and POSTs need the header
  `X-WallyCull: 1`, so other web pages cannot trigger actions.
- Bump `ANALYSIS_VERSION` in `wallycull/__init__.py` when measurements change; cached images are then
  analyzed again automatically.

## Installed layout

The setup bundles Python 3.12 (NuGet package) with `requirements.txt` in `{app}\python`.
`WallyCull.exe` is a copy of `pythonw.exe` with our icon, started as `WallyCull.exe -m wallycull`.
The AI packages (about 3 GB) live in `%LOCALAPPDATA%\WallyCull\ai-packages`, so updates stay small.
The setup's last step runs `python -m wallycull setup-ai`, which only downloads when
`requirements-ai.txt`, the GPU or the Python version changed.

## Release process

1. Make the change, add an entry at the top of `CHANGELOG.md` (`## <version> - <date>`).
2. Bump `__version__` in `wallycull/__init__.py`.
3. Push to `main`.

`.github/workflows/setup.yml` sees that release `v<version>` does not exist yet and then:
writes `wallycull/build.json` (repo id, so updates survive a repo rename), bundles Python, builds
`WallyCull.exe`, compiles the setup, installs it silently as a test, and publishes the release with the
changelog section as notes. Installed apps pick it up within 6 hours or at their next start.

Pushes without a version bump build nothing. **Run workflow** in the Actions tab rebuilds the current version.
