"""Self-update: fetches the newest setup from the private GitHub repo's releases and installs it silently.

The private repo needs a GitHub sign-in once. It comes from the Git Credential Manager (one click in the
browser) or from a read-only token you paste into the app. Your data lives apart from the program and
survives every update.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from . import __version__
from .paths import INSTALLED, USER_DIR

REPO = "71NikoW/foto-aussortierer"
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
TOKEN_FILE = USER_DIR / "github-token.txt"
LEGACY_TOKEN_FILE = USER_DIR / "github-schluessel.txt"
TOKEN_LINK = (
    "https://github.com/settings/personal-access-tokens/new?name=WallyCull-Updates"
    "&description=Read-only+access+for+WallyCull+updates&target_name=71NikoW&expires_in=none&contents=read"
)


def _build_info() -> dict:
    """Written by the release build: the repo's numeric id still works after a rename."""
    try:
        return json.loads((Path(__file__).with_name("build.json")).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def api_base() -> str:
    repo_id = _build_info().get("repo_id")
    return f"https://api.github.com/repositories/{repo_id}" if repo_id else f"https://api.github.com/repos/{REPO}"


def _git_path() -> str | None:
    found = shutil.which("git")
    if found:
        return found
    for base in (os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
        for part in ("Git/cmd/git.exe", "Programs/Git/cmd/git.exe"):
            if base and (Path(base) / part).exists():
                return str(Path(base) / part)
    return None


def _git_credential(ask: bool) -> str | None:
    """Asks the Git Credential Manager for GitHub access (ask=True opens the sign-in if needed)."""
    git = _git_path()
    if not git:
        return None
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    if not ask:
        env["GCM_INTERACTIVE"] = "never"
    query = "protocol=https\nhost=github.com\n\n"
    try:
        r = subprocess.run([git, "credential", "fill"], input=query, capture_output=True, text=True,
                           timeout=300 if ask else 20, env=env, creationflags=NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return None
    values = dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)
    if r.returncode or not values.get("password"):
        return None
    if ask:  # only stored after an explicit sign-in
        subprocess.run([git, "credential", "approve"], input=r.stdout + "\n", text=True, env=env, creationflags=NO_WINDOW)
    return values["password"]


def _token(ask: bool = False) -> str | None:
    if os.environ.get("WALLYCULL_GITHUB_TOKEN"):
        return os.environ["WALLYCULL_GITHUB_TOKEN"]
    for path in (TOKEN_FILE, LEGACY_TOKEN_FILE):
        if path.exists():
            text = path.read_text(encoding="utf-8").strip()
            if text:
                return text
    return _git_credential(ask)


def save_token(text: str) -> None:
    text = text.strip()
    if not re.fullmatch(r"(github_pat_|ghp_|gho_)[A-Za-z0-9_]{20,}", text):
        raise ValueError("That does not look like a GitHub token (it starts with github_pat_).")
    USER_DIR.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(text, encoding="utf-8")


def _request(url: str, token: str, binary: bool = False):
    req = urllib.request.Request(url, headers={"Accept": "application/octet-stream" if binary else "application/vnd.github+json",
                                               "User-Agent": "WallyCull", "X-GitHub-Api-Version": "2022-11-28"})
    # Not forwarded to the download redirect (S3), which would reject it
    req.add_unredirected_header("Authorization", f"Bearer {token}")
    return urllib.request.urlopen(req, timeout=60)


def version_tuple(text: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", text)[:3])


def _releases(token: str) -> list[dict]:
    with _request(f"{api_base()}/releases?per_page=10", token) as r:
        data = json.loads(r.read())
    result = []
    for rel in data:
        if rel.get("draft") or rel.get("prerelease"):
            continue
        setup = next((a for a in rel.get("assets", []) if a["name"].lower().endswith(".exe")), None)
        if setup:
            result.append({"version": rel["tag_name"].lstrip("v"), "asset": setup["url"], "name": setup["name"],
                           "size": setup["size"], "notes": (rel.get("body") or "")[:800]})
    return sorted(result, key=lambda r: version_tuple(r["version"]), reverse=True)


def check(ask: bool = False) -> dict:
    """Status: development | sign_in | current | new (with the newest and the previous release)."""
    if not INSTALLED:
        return {"status": "development", "current": __version__}
    token = _token(ask)
    sign_in = {"status": "sign_in", "current": __version__, "git": bool(_git_path()), "link": TOKEN_LINK}
    if not token:
        return sign_in
    try:
        releases = _releases(token)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 404):
            return {**sign_in, "error": "GitHub denied access."}
        raise
    if not releases:
        return {"status": "current", "current": __version__}
    newest = releases[0]
    status = "new" if version_tuple(newest["version"]) > version_tuple(__version__) else "current"
    previous = next((r for r in releases if version_tuple(r["version"]) < version_tuple(__version__)), None)
    return {"status": status, "current": __version__, "new": newest["version"], "_release": newest,
            "previous": previous["version"] if previous else None, "_previous": previous, "notes": newest["notes"]}


def download(release: dict, log=print) -> Path:
    token = _token()
    if not token:
        raise RuntimeError("no GitHub sign-in")
    target = Path(tempfile.gettempdir()) / release["name"]
    log(f"Downloading {release['name']} ({release['size'] / 1048576:.0f} MB) ...")
    with _request(release["asset"], token, binary=True) as r, open(target, "wb") as f:
        shutil.copyfileobj(r, f, 1024 * 1024)
    if target.stat().st_size != release["size"]:
        raise RuntimeError("download incomplete")
    return target


def run_setup(setup: Path) -> None:
    """Starts the setup invisibly. It closes the app, updates it and starts it again."""
    subprocess.Popen([str(setup), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS"],
                     cwd=str(setup.parent), creationflags=(0x00000008 | 0x00000200) if sys.platform == "win32" else 0)


def python_exe() -> str:
    """python.exe in the program folder: for background tasks whose output the app reads."""
    path = Path(sys.executable)
    if path.name.lower() != "python.exe" and (path.parent / "python.exe").exists():
        return str(path.parent / "python.exe")
    return sys.executable
