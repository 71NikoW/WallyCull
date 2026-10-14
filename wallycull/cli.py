"""Command line: python -m wallycull <command> "D:\\Photos"  (without a command the app window opens)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__, ai, analyze, classify, config, files, learning, report


def _folder(path: str) -> Path:
    p = Path(path).expanduser().resolve()
    if not p.is_dir():
        sys.exit(f"Folder not found: {p}")
    return p


def _print_counts(counts: dict) -> None:
    print("Result: " + ", ".join(f"{classify.NAMES[c]}: {counts[c]}" for c in classify.CATEGORIES))


def main(argv: list[str] | None = None) -> None:
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8")
            except Exception:
                pass

    p = argparse.ArgumentParser(prog="wallycull", description="WallyCull: rate quality and value, cull photos safely.")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--config", help="your own settings file (overrides config.yaml)")
    sub = p.add_subparsers(dest="command", required=True)

    ap = sub.add_parser("app", help="open the app window (default when no command is given)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-window", action="store_true", help=argparse.SUPPRESS)

    a = sub.add_parser("analyze", help="measure images, classify them, create the report (touches no files)")
    a.add_argument("folder")
    a.add_argument("--recompute", action="store_true", help="ignore the cache and measure everything again")
    a.add_argument("--no-browser", action="store_true", help="do not open the report automatically")
    a.add_argument("--no-ai", action="store_true", help="skip the AI models this time")

    c = sub.add_parser("classify", help="only classify again (e.g. after changing settings)")
    c.add_argument("folder")
    c.add_argument("--no-browser", action="store_true")

    r = sub.add_parser("reject", help="move rejected images to _rejected (default: dry run)")
    r.add_argument("folder")
    r.add_argument("--execute", action="store_true", help="really move the files instead of only listing them")

    re_ = sub.add_parser("restore", help="put images from _rejected back")
    re_.add_argument("folder")
    re_.add_argument("--file", help="only images whose path contains this text (e.g. DSCF1234)")
    re_.add_argument("--run", help="only images of one run (date_time)")
    re_.add_argument("--image-id", type=int, action="append", help=argparse.SUPPRESS)
    re_.add_argument("--all", action="store_true", help="restore everything without asking")

    pu = sub.add_parser("purge", help="send the contents of _rejected to the Recycle Bin (asks first)")
    pu.add_argument("folder")
    pu.add_argument("--confirmed", action="store_true", help=argparse.SUPPRESS)

    ra = sub.add_parser("rate", help="rate images Tinder-style in the browser, the model learns along")
    ra.add_argument("folder")
    ra.add_argument("--port", type=int, default=8765)

    t = sub.add_parser("train", help="train the model from all ratings and classify again")
    t.add_argument("folder")

    n = sub.add_parser("annotations", help="export your notes (wrongly analyzed images) as CSV")
    n.add_argument("folder")

    rp = sub.add_parser("report", help="create and open the report again")
    rp.add_argument("folder")

    s = sub.add_parser("setup-ai", help="install PyTorch (GPU), CLIP and the AI models")
    s.add_argument("--force", action="store_true", help="reinstall even if everything is there")
    sub.add_parser("download-models", help=argparse.SUPPRESS)

    argv = sys.argv[1:] if argv is None else argv
    args = p.parse_args(argv or ["app"])
    cfg = config.load(args.config)

    if args.command == "app":
        from . import app

        return app.start(cfg, port=args.port, window=not args.no_window)
    if args.command == "setup-ai":
        from . import ai_setup

        sys.exit(ai_setup.install(force=args.force))
    if args.command == "download-models":
        from . import ai_setup

        return ai_setup.download_models(cfg)
    folder = _folder(args.folder)

    if args.command == "analyze":
        try:
            stats = analyze.run(folder, cfg, recompute=args.recompute)
        except KeyboardInterrupt:
            sys.exit(1)
        if stats["errors"]:
            print(f"{stats['errors']} files could not be read (listed in the report under 'Review').")
        if not args.no_ai:
            try:
                ai.run(folder, cfg, recompute=args.recompute)
            except KeyboardInterrupt:
                sys.exit(1)
            except Exception as e:  # AI problems must not block the classic analysis
                print(f"AI analysis skipped: {type(e).__name__}: {e}")
    if args.command in ("analyze", "classify"):
        _print_counts(classify.run(folder, cfg))
        target = report.create(folder, open_browser=not args.no_browser)
        print(f"Report: {target}")
        print("No files were changed. Next step: 'reject' (first only shows what would happen).")
    elif args.command == "reject":
        files.move_rejected(folder, cfg, execute=args.execute)
        if args.execute:
            report.create(folder)
    elif args.command == "restore":
        if not args.file and not args.run and not args.all and not args.image_id:
            answer = input("Really restore ALL rejected images? (y/n) ").strip().lower()
            if answer not in ("y", "yes"):
                sys.exit("Cancelled.")
        if files.restore(folder, cfg, file=args.file, run=args.run, image_ids=args.image_id):
            classify.run(folder, cfg)
            report.create(folder)
    elif args.command == "purge":
        files.purge(folder, cfg, confirmed=args.confirmed)
    elif args.command == "rate":
        from . import rating

        rating.serve(folder, cfg, port=args.port)
    elif args.command == "train":
        status = learning.train(folder, cfg)
        if status["active"]:
            print(f"Model trained from {status['rated']} ratings, accuracy {status['accuracy'] * 100:.0f}%.")
        else:
            print(status["hint"])
        _print_counts(classify.run(folder, cfg))
        report.create(folder)
    elif args.command == "annotations":
        print(f"Saved: {report.export_annotations(folder)}")
    elif args.command == "report":
        print(report.create(folder, open_browser=True))
