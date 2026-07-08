import argparse
import csv
import json
import os
import re
import shutil
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen

from mutagen.mp4 import MP4

INVALID_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}

ARTIST_CANDIDATES = ["artist", "albumartist", "album-artist", "album_artist"]
ALBUM_CANDIDATES = ["album"]
SERIES_CANDIDATES = ["series"]
SERIES_PART_CANDIDATES = ["series-part", "series_part", "seriespart", "series_sequence", "series-sequence", "seriessequence"]
YEAR_CANDIDATES = ["year"]
ASIN_CANDIDATES = ["asin", "ASIN"]
TRACK_TAG_KEYS = ("trkn", "track")


def load_env(path: Path = Path(".env")) -> dict:
    """Load simple KEY=VALUE pairs from a local .env file."""
    values = dict(os.environ)
    if not path.exists():
        return values

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("\"'")
    return values


def get_env_value(env: dict, *names: str) -> str:
    for name in names:
        value = env.get(name)
        if value:
            return value
    return ""


def get_abs_config(env: dict) -> tuple[str, str, str]:
    base_url = get_env_value(env, "AUDIOBOOKSHELF_URL", "ABS_URL")
    library_id = get_env_value(env, "AUDIOBOOKSHELF_LIBRARY_ID", "ABS_LIBRARY_ID")
    old_library_url = get_env_value(env, "AUDIOBOOKSHELF_LIBRARY_URL", "ABS_LIBRARY_URL")

    if old_library_url:
        print(
            "Warning: AUDIOBOOKSHELF_LIBRARY_URL/ABS_LIBRARY_URL is deprecated; "
            "use AUDIOBOOKSHELF_URL and AUDIOBOOKSHELF_LIBRARY_ID instead.",
            file=sys.stderr,
        )
        old_base_url, old_library_id = split_legacy_library_url(old_library_url)
        base_url = base_url or old_base_url
        library_id = library_id or old_library_id

    api_key = get_env_value(
        env, "AUDIOBOOKSHELF_API_KEY", "ABS_API_KEY", "AUDIOBOOKSHELF_TOKEN", "ABS_TOKEN"
    )
    return normalize_abs_base_url(base_url), library_id.strip(), api_key


def normalize_abs_base_url(base_url: str) -> str:
    return base_url.strip().rstrip("/")


def split_legacy_library_url(library_url: str) -> tuple[str, str]:
    parsed = urlparse(library_url.strip())
    path_parts = [part for part in parsed.path.split("/") if part]

    try:
        api_index = path_parts.index("api")
    except ValueError:
        return library_url.rstrip("/"), ""

    if path_parts[api_index + 1:api_index + 2] != ["libraries"] or len(path_parts) <= api_index + 2:
        return library_url.rstrip("/"), ""

    base_path = "/" + "/".join(path_parts[:api_index]) if api_index else ""
    base_url = urlunparse((parsed.scheme, parsed.netloc, base_path.rstrip("/"), "", "", ""))
    return base_url.rstrip("/"), path_parts[api_index + 2]


def build_abs_url(base_url: str, *path_parts: str, query: dict | None = None) -> str:
    path = "/".join(str(part).strip("/") for part in path_parts if str(part).strip("/"))
    url = f"{base_url.rstrip('/')}/{path}"
    if query:
        url = f"{url}?{urlencode(query)}"
    return url


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


def has_track_tag(path: Path) -> bool:
    audio = MP4(str(path))
    tags = audio.tags or {}
    return any(key in tags and tags[key] for key in TRACK_TAG_KEYS)


def clear_track_tag(path: Path) -> bool:
    audio = MP4(str(path))
    if audio.tags is None:
        return False

    changed = False
    for key in TRACK_TAG_KEYS:
        if key in audio.tags and audio.tags[key]:
            del audio.tags[key]
            changed = True

    if changed:
        audio.save()
    return changed


class AudiobookshelfRequestError(RuntimeError):
    def __init__(self, url: str, status: int | None = None, response_body: str = ""):
        self.url = url
        self.status = status
        self.response_body = response_body
        super().__init__(self.format_message())

    def format_message(self) -> str:
        parts = ["Audiobookshelf request failed.", "", "URL:", self.url]
        if self.status is not None:
            parts.extend(["", f"HTTP {self.status}"])
        if self.response_body:
            parts.extend(["", "Response:", self.response_body])
        return "\n".join(parts)


class AudiobookshelfClient:
    def __init__(self, base_url: str, library_id: str, api_key: str):
        self.base_url = base_url
        self.library_id = library_id
        self.api_key = api_key

    def require_config(self) -> None:
        if not self.base_url or not self.library_id or not self.api_key:
            raise ValueError(
                "missing AUDIOBOOKSHELF_URL, AUDIOBOOKSHELF_LIBRARY_ID, "
                "or AUDIOBOOKSHELF_API_KEY in .env"
            )

    def library_url(self, *path_parts: str, query: dict | None = None) -> str:
        return build_abs_url(
            self.base_url, "api", "libraries", self.library_id, *path_parts, query=query
        )

    def request_json(self, url: str) -> dict:
        request = Request(
            url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
            },
        )
        try:
            with urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise AudiobookshelfRequestError(url, exc.code, body) from exc

    def iter_items(self):
        page = 0
        while True:
            url = self.library_url("items", query={"limit": 100, "page": page})
            data = self.request_json(url)
            items = data.get("results") or data.get("items") or data.get("libraryItems") or []
            for item in items:
                yield item

            total = data.get("total")
            if total is None or (page + 1) * 100 >= total or not items:
                break
            page += 1

    def search_items(self, query: str, limit: int = 10):
        data = self.request_json(self.library_url("search", query={"q": query, "limit": limit}))
        results = data.get("book") or data.get("podcast") or []
        for result in results:
            item = result.get("libraryItem") or result
            if item:
                yield item


def item_asin(item: dict) -> str:
    media = item.get("media") or {}
    metadata = media.get("metadata") or item.get("mediaMetadata") or {}
    return str(metadata.get("asin") or "").strip().lower()


def find_duplicate_asin(client: AudiobookshelfClient, asin: str) -> dict | None:
    client.require_config()
    normalized_asin = asin.strip().lower()

    # Audiobookshelf documents library search but not a native ASIN-only filter.
    # Query by ASIN first to avoid downloading large libraries, then validate the
    # returned metadata exactly because search can match fields other than ASIN.
    for item in client.search_items(asin):
        if item_asin(item) == normalized_asin:
            return item

    # Keep the previous exact behavior as a conservative fallback in case a server
    # version does not index ASINs in library search results.
    for item in client.iter_items():
        if item_asin(item) == normalized_asin:
            return item
    return None


def format_duplicate(item: dict) -> str:
    media = item.get("media") or {}
    metadata = media.get("metadata") or item.get("mediaMetadata") or {}
    title = metadata.get("title") or item.get("relPath") or item.get("path") or item.get("id") or "unknown item"
    return f"duplicate ASIN already exists in Audiobookshelf: {title}"


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


def process_file(path: Path, target_root: Path, abs_client: AudiobookshelfClient) -> tuple:
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

    if not tags["asin"]:
        return None, "missing ASIN"

    try:
        duplicate = find_duplicate_asin(abs_client, tags["asin"])
    except AudiobookshelfRequestError as exc:
        return None, f"failed to check Audiobookshelf duplicates: {exc}"
    except (URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        return None, f"failed to check Audiobookshelf duplicates: {exc}"

    if duplicate:
        return None, format_duplicate(duplicate)

    target_path = build_target_path(target_root, tags, path.suffix.lower())
    return target_path, None


def run(source: Path, target: Path, dryrun: bool, csv_path: Path) -> None:
    files = list(find_m4b_files(source))
    if not files:
        print(f"No .m4b files found under {source}")
        return

    env = load_env()
    base_url, library_id, api_key = get_abs_config(env)
    abs_client = AudiobookshelfClient(base_url, library_id, api_key)

    rows = []
    moved = 0
    skipped = 0

    for path in files:
        target_path, reason = process_file(path, target, abs_client)

        if reason is not None:
            skipped += 1
            rows.append([str(path), "", "skip", reason])
            print(f"SKIP  {path}  ({reason})")
            continue

        try:
            track_tag_present = has_track_tag(path)
        except Exception as exc:
            skipped += 1
            rows.append([str(path), str(target_path), "skip", f"failed to check track tag: {exc}"])
            print(f"SKIP  {path}  (failed to check track tag: {exc})")
            continue

        if dryrun:
            moved += 1
            reason = "track tag would be removed" if track_tag_present else ""
            rows.append([str(path), str(target_path), "move", reason])
            continue

        if target_path.exists():
            skipped += 1
            rows.append([str(path), str(target_path), "skip", "destination already exists"])
            print(f"SKIP  {path}  (destination already exists: {target_path})")
            continue

        if track_tag_present:
            try:
                clear_track_tag(path)
            except Exception as exc:
                skipped += 1
                rows.append([str(path), str(target_path), "skip", f"failed to clear track tag: {exc}"])
                print(f"SKIP  {path}  (failed to clear track tag: {exc})")
                continue
            print(f"CLEAR {path}  (track tag removed)")

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
