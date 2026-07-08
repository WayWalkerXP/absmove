import argparse
import csv
import re
import shutil
import sys
from pathlib import Path

from mutagen.mp4 import MP4

INVALID_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}

ARTIST_CANDIDATES = ["artist", "albumartist", "album-artist", "album_artist"]
ALBUM_CANDIDATES = ["album"]
SERIES_CANDIDATES = ["series"]
SERIES_PART_CANDIDATES = ["series-part", "series_part", "seriespart", "series_sequence", "series-sequence", "seriessequence"]
YEAR_CANDIDATES = ["year"]
ASIN_CANDIDATES = ["asin"]


def sanitize_component(name: str) -> str:
    """Make a single path component safe on both Windows and Linux filesystems."""
    name = INVALID_CHARS_RE.sub("_", name)
    name = name.strip().rstrip(". ")
    if not name:
        name = "_"
    if name.upper() in RESERVED_NAMES:
        name = f"_{name}"
    return name


def resolve_tag(tags: dict, *candidates: str) -> str:
    for candidate in candidates:
        value = tags.get(candidate.lower())
        if value and value.strip():
            return value.strip()
    return ""


def read_raw_tags(path: Path) -> dict:
    """Read all tags from an m4b file into a lowercased name -> value dict."""
    audio = MP4(str(path))
    mp4_tags = audio.tags or {}
    result: dict = {}

    for key, value in mp4_tags.items():
        if key.startswith("----:") and value:
            name = key.split(":")[-1]
            raw = value[0]
            if isinstance(raw, (bytes, bytearray)):
                raw = raw.decode("utf-8", errors="replace")
            result[name.lower()] = str(raw).strip()

    def first_standard(atom: str) -> str:
        value = mp4_tags.get(atom)
        if value:
            return str(value[0]).strip()
        return ""

    artist = first_standard("\xa9ART")
    if artist:
        result["artist"] = artist

    albumartist = first_standard("aART")
    if albumartist:
        result["albumartist"] = albumartist

    album = first_standard("\xa9alb")
    if album:
        result["album"] = album

    date_value = first_standard("\xa9day")
    if date_value:
        match = re.match(r"(\d{4})", date_value)
        result["year"] = match.group(1) if match else date_value

    return result


def format_series_part(value: str) -> str:
    if not value:
        return value
    try:
        return f"{int(value):02d}"
    except ValueError:
        return value


def resolve_book_tags(raw_tags: dict) -> dict:
    return {
        "artist": resolve_tag(raw_tags, *ARTIST_CANDIDATES),
        "album": resolve_tag(raw_tags, *ALBUM_CANDIDATES),
        "series": resolve_tag(raw_tags, *SERIES_CANDIDATES),
        "series_part": format_series_part(resolve_tag(raw_tags, *SERIES_PART_CANDIDATES)),
        "year": resolve_tag(raw_tags, *YEAR_CANDIDATES),
        "asin": resolve_tag(raw_tags, *ASIN_CANDIDATES),
    }


def build_target_path(target_root: Path, tags: dict, ext: str) -> Path:
    artist = sanitize_component(tags["artist"])

    parts = [artist]
    if tags["series"]:
        parts.append(sanitize_component(tags["series"]))

    if tags["series_part"]:
        book_folder = f"{tags['series_part']} - {tags['album']}"
    else:
        book_folder = tags["album"]
    parts.append(sanitize_component(book_folder))

    filename = tags["album"]
    if tags["year"]:
        filename += f" ({tags['year']})"
    if tags["asin"]:
        filename += f" [{tags['asin']}]"
    filename = sanitize_component(filename) + ext

    return target_root.joinpath(*parts, filename)


def find_m4b_files(source: Path):
    for path in sorted(source.rglob("*")):
        if path.is_file() and path.suffix.lower() == ".m4b":
            yield path


def process_file(path: Path, target_root: Path) -> tuple:
    """Returns (target_path_or_None, reason_if_skipped)."""
    try:
        raw_tags = read_raw_tags(path)
    except Exception as exc:
        return None, f"failed to read tags: {exc}"

    tags = resolve_book_tags(raw_tags)

    if not tags["artist"]:
        return None, "missing artist/author"
    if not tags["album"]:
        return None, "missing album"

    target_path = build_target_path(target_root, tags, path.suffix.lower())
    return target_path, None


def run(source: Path, target: Path, dryrun: bool, csv_path: Path) -> None:
    files = list(find_m4b_files(source))
    if not files:
        print(f"No .m4b files found under {source}")
        return

    rows = []
    moved = 0
    skipped = 0

    for path in files:
        target_path, reason = process_file(path, target)

        if reason is not None:
            skipped += 1
            rows.append([str(path), "", "skip", reason])
            print(f"SKIP  {path}  ({reason})")
            continue

        if dryrun:
            moved += 1
            rows.append([str(path), str(target_path), "move", ""])
            continue

        if target_path.exists():
            skipped += 1
            rows.append([str(path), str(target_path), "skip", "destination already exists"])
            print(f"SKIP  {path}  (destination already exists: {target_path})")
            continue

        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(target_path))
        moved += 1
        rows.append([str(path), str(target_path), "move", ""])
        print(f"MOVE  {path}  ->  {target_path}")

    if dryrun:
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["source", "target", "action", "reason"])
            writer.writerows(rows)
        print(f"\nDry run complete. {moved} file(s) would move, {skipped} skipped.")
        print(f"Results written to {csv_path}")
    else:
        print(f"\nDone. {moved} file(s) moved, {skipped} skipped.")


def main():
    parser = argparse.ArgumentParser(description="Move .m4b audiobook files into an organized target directory based on their tags.")
    parser.add_argument("--source", required=True, type=Path, help="Source directory to scan for .m4b files")
    parser.add_argument("--target", required=True, type=Path, help="Target directory to move .m4b files into")
    parser.add_argument("--dryrun", action="store_true", help="Simulate the move and write results to a CSV instead of moving files")
    parser.add_argument("--csv", type=Path, default=Path("dryrun_results.csv"), help="Path to the CSV file written in --dryrun mode (default: dryrun_results.csv)")
    args = parser.parse_args()

    if not args.source.is_dir():
        print(f"Error: source directory does not exist: {args.source}", file=sys.stderr)
        sys.exit(1)

    run(args.source.resolve(), args.target.resolve(), args.dryrun, args.csv)


if __name__ == "__main__":
    main()
