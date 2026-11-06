#!/usr/bin/env python3
"""
One-off migration script: renames existing download folders to match the
current naming convention.

  youtube channel folders:  Name____Name        -> Name
  youtube playlist folders: Owner____Playlist    -> unchanged (left as-is)
  twitter folders:          @Handle____Display   -> @Handle

Dry-run by default (just prints the plan). Pass --apply to actually perform
the renames. If a target folder name already exists (e.g. two old folders
now collapse onto the same name), files are merged into it instead of
overwriting, with " (1)", " (2)", ... suffixes on any name collisions.

Usage:
  python3 rename_backlog.py [--downloads-dir PATH] [--apply]
"""
import argparse
import os
import shutil


def plan_youtube_renames(youtube_dir):
    renames = []
    for name in sorted(os.listdir(youtube_dir)):
        full = os.path.join(youtube_dir, name)
        if not os.path.isdir(full):
            continue
        if "____" not in name:
            continue
        parts = name.split("____", 1)
        if len(parts) == 2 and parts[0] == parts[1] and parts[0]:
            new_name = parts[0]
            if new_name != name:
                renames.append((full, os.path.join(youtube_dir, new_name)))
    return renames


def plan_twitter_renames(twitter_dir):
    renames = []
    for name in sorted(os.listdir(twitter_dir)):
        full = os.path.join(twitter_dir, name)
        if not os.path.isdir(full):
            continue

        # Strip a "____DisplayName" suffix if present, and ensure a leading
        # "@" - covers "@handle____Display", "handle____Display", and bare
        # "handle" (no "@", no suffix) folders alike.
        handle = name.split("____", 1)[0] if "____" in name else name
        if not handle:
            continue
        new_name = handle if handle.startswith("@") else f"@{handle}"

        if new_name != name:
            renames.append((full, os.path.join(twitter_dir, new_name)))
    return renames


def merge_or_rename(src, dst, apply):
    if os.path.exists(dst):
        print(f"  MERGE   {src}\n          -> {dst}  (target already exists)")
        if apply:
            for item in os.listdir(src):
                s = os.path.join(src, item)
                d = os.path.join(dst, item)
                if os.path.exists(d):
                    base, ext = os.path.splitext(item)
                    i = 1
                    while os.path.exists(d):
                        d = os.path.join(dst, f"{base} ({i}){ext}")
                        i += 1
                    print(f"          conflict: {item} -> {os.path.basename(d)}")
                shutil.move(s, d)
            os.rmdir(src)
    else:
        print(f"  RENAME  {src}\n          -> {dst}")
        if apply:
            os.rename(src, dst)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--downloads-dir",
        default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "web_files", "downloads"),
        help="Path to the downloads directory (default: web_files/downloads next to this script)"
    )
    parser.add_argument("--apply", action="store_true", help="Actually perform the renames (default: dry-run only)")
    args = parser.parse_args()

    youtube_dir = os.path.join(args.downloads_dir, "youtube")
    twitter_dir = os.path.join(args.downloads_dir, "twitter")

    all_renames = []
    if os.path.isdir(youtube_dir):
        all_renames += [("youtube", s, d) for s, d in plan_youtube_renames(youtube_dir)]
    else:
        print(f"[skip] {youtube_dir} does not exist")

    if os.path.isdir(twitter_dir):
        all_renames += [("twitter", s, d) for s, d in plan_twitter_renames(twitter_dir)]
    else:
        print(f"[skip] {twitter_dir} does not exist")

    if not all_renames:
        print("Nothing to rename.")
        return

    print(f"{'APPLYING' if args.apply else 'DRY RUN'} - {len(all_renames)} folder(s) to rename:\n")
    for category, src, dst in all_renames:
        print(f"[{category}]")
        merge_or_rename(src, dst, args.apply)
        print()

    if not args.apply:
        print("Dry run only - nothing was changed. Re-run with --apply to perform these renames.")


if __name__ == "__main__":
    main()
