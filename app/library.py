from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import Settings
from .naming import (
    LOSSLESS_EXTENSIONS,
    extension_of,
    folder_hint,
    normalize,
    parse_path,
)
from .torbox import KINDS, TorBox, TorBoxError

log = logging.getLogger("library")

PREFIX = {"torrents": "t", "usenet": "u", "webdl": "w"}
PREFIX_KIND = {value: key for key, value in PREFIX.items()}

# Rows BitChord sees. It does not pass a limit, and a huge answer helps nobody.
MAX_RESULTS = 50


@dataclass(frozen=True)
class Track:
    id: str
    kind: str
    item_id: int
    file_id: int
    path: str
    title: str
    artist: str
    album: str
    ext: str
    size: int

    @property
    def lossless(self) -> bool:
        return self.ext in LOSSLESS_EXTENSIONS

    @property
    def search_text(self) -> str:
        return normalize(f"{self.title} {self.artist} {self.album} {self.path}")

    @property
    def title_norm(self) -> str:
        return normalize(self.title)

    @property
    def artist_norm(self) -> str:
        return normalize(self.artist)


def _as_int(value: object) -> int | None:
    try:
        return int(float(value))  # TorBox sends ids as numbers; tolerate strings.
    except (TypeError, ValueError):
        return None


def _state_allows_streaming(item: dict) -> bool:
    state = str(item.get("download_state") or "").lower()
    if state in {
        "downloading",
        "metadl",
        "paused",
        "stalled (no seeds)",
        "checkingresumedata",
        "queued",
        "error",
    }:
        return False
    if item.get("download_finished") is False and item.get("download_present") is False:
        return False
    return True


def _track_from_file(kind: str, item: dict, file: dict, allowed: frozenset[str]) -> Track | None:
    item_id = _as_int(item.get("id"))
    file_id = _as_int(file.get("id"))
    if item_id is None or file_id is None:
        return None

    raw_path = (
        file.get("name")
        or file.get("short_name")
        or file.get("s3_path")
        or ""
    )
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None

    item_name = item.get("name")
    if isinstance(item_name, str) and item_name.strip() and not any(
        separator in raw_path for separator in ("/", "\\")
    ):
        raw_path = f"{item_name}/{raw_path}"

    ext = extension_of(raw_path)
    if ext not in allowed:
        return None

    parsed = parse_path(raw_path)
    if not parsed.title:
        return None

    # `Album/01 - Title.flac` under a torrent named `Artist - Album` has no
    # artist in the file path. The release name does.
    item_artist, item_album = folder_hint(item_name) if isinstance(item_name, str) else ("", "")
    if item_artist and (not parsed.artist or (not parsed.album and parsed.artist == item_album)):
        parsed = parsed.__class__(parsed.title, item_artist, parsed.album or item_album)
    elif item_album and not parsed.album:
        parsed = parsed.__class__(parsed.title, parsed.artist, item_album)

    size = _as_int(file.get("size")) or 0
    if size <= 0:
        return None

    return Track(
        id=f"{PREFIX[kind]}-{item_id}-{file_id}",
        kind=kind,
        item_id=item_id,
        file_id=file_id,
        path=raw_path,
        title=parsed.title,
        artist=parsed.artist,
        album=parsed.album,
        ext=ext,
        size=size,
    )


def parse_track_id(track_id: str) -> tuple[str, int, int] | None:
    parts = track_id.split("-")
    if len(parts) != 3:
        return None
    kind = PREFIX_KIND.get(parts[0])
    item_id = _as_int(parts[1])
    file_id = _as_int(parts[2])
    if kind is None or item_id is None or file_id is None:
        return None
    return kind, item_id, file_id


class Library:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._tracks: list[Track] = []
        self._by_id: dict[str, Track] = {}
        self._search_rows: list[tuple[Track, str, str, str]] = []
        self._refresh_lock = asyncio.Lock()
        self.last_refresh: float | None = None
        self.last_attempt: float | None = None
        self.errors: dict[str, str] = {}
        self._load()

    # ── persistence ──────────────────────────────────────────────────────

    @property
    def _path(self) -> Path:
        return self._settings.data_dir / "index.json"

    def _load(self) -> None:
        path = self._path
        if not path.is_file():
            return
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
            tracks = [Track(**row) for row in body.get("tracks", [])]
        except (OSError, ValueError, TypeError) as exc:
            log.warning("could not read %s: %s", path, exc)
            return
        self._replace(tracks)
        self.last_refresh = body.get("last_refresh")
        log.info("loaded %d tracks from %s", len(tracks), path)

    def _save(self) -> None:
        path = self._path
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        body = {
            "version": 1,
            "last_refresh": self.last_refresh,
            "tracks": [asdict(track) for track in self._tracks],
        }
        try:
            temporary.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
            temporary.replace(path)
        except OSError as exc:
            log.warning("could not save %s: %s", path, exc)

    def _replace(self, tracks: list[Track]) -> None:
        # Two torrents holding the same release produce identical rows. Keep
        # one so one copy does not crowd out the rest of the search results.
        seen: set[tuple[str, str, str, int]] = set()
        unique: list[Track] = []
        for track in tracks:
            key = (track.title_norm, track.artist_norm, normalize(track.album), track.size)
            if key in seen:
                continue
            seen.add(key)
            unique.append(track)
        unique.sort(key=lambda track: (track.artist_norm, normalize(track.album), track.title_norm))
        self._tracks = unique
        self._by_id = {track.id: track for track in unique}
        self._search_rows = [
            (track, track.search_text, track.title_norm, track.artist_norm)
            for track in unique
        ]

    # ── refresh ──────────────────────────────────────────────────────────

    @property
    def tracks(self) -> list[Track]:
        return self._tracks

    def get(self, track_id: str) -> Track | None:
        return self._by_id.get(track_id)

    async def refresh(self, torbox: TorBox) -> None:
        if self._refresh_lock.locked():
            return
        async with self._refresh_lock:
            self.last_attempt = time.time()
            errors: dict[str, str] = {}
            fresh: dict[str, list[Track]] = {}
            succeeded: list[str] = []

            for kind in self._settings.kinds:
                try:
                    items = await torbox.list_items(
                        kind,
                        page_size=self._settings.page_size,
                        max_pages=self._settings.max_pages,
                    )
                except TorBoxError as exc:
                    errors[kind] = str(exc)
                    log.warning("%s list failed: %s", kind, exc)
                    continue

                succeeded.append(kind)
                kind_tracks: list[Track] = []
                for item in items:
                    if not _state_allows_streaming(item):
                        continue
                    files = item.get("files")
                    if not isinstance(files, list):
                        continue
                    for file in files:
                        if not isinstance(file, dict):
                            continue
                        track = _track_from_file(kind, item, file, self._settings.audio_extensions)
                        if track is not None:
                            kind_tracks.append(track)
                fresh[kind] = kind_tracks
                log.info("%s: %d items, %d audio files", kind, len(items), len(kind_tracks))

            if not succeeded:
                self.errors = errors
                log.warning("refresh failed; keeping the previous index")
                return

            # A collection that failed keeps its old rows rather than vanishing
            # from the library because one endpoint had a bad minute.
            old_by_kind: dict[str, list[Track]] = {}
            for track in self._tracks:
                old_by_kind.setdefault(track.kind, []).append(track)

            merged: list[Track] = []
            for kind in self._settings.kinds:
                merged.extend(fresh.get(kind, old_by_kind.get(kind, [])))
            self._replace(merged)
            self.errors = errors
            self.last_refresh = time.time()
            self._save()
            log.info("index ready: %d playable tracks", len(self._tracks))

    # ── search ───────────────────────────────────────────────────────────

    def search(self, query: str, limit: int = MAX_RESULTS) -> list[Track]:
        query_norm = normalize(query)
        if not query_norm:
            return []
        tokens = query_norm.split()

        scored: list[tuple[float, Track]] = []
        for track, haystack, title, artist in self._search_rows:
            matched = [token for token in tokens if token in haystack]
            if not matched:
                continue
            # Every word is the strong answer; a partial answer is still useful
            # when the catalogue disagrees about an article or a credit.
            if len(matched) < len(tokens) and len(matched) * 2 < len(tokens):
                continue
            scored.append(
                (self._score(track, query_norm, tokens, title, artist), track)
            )

        scored.sort(key=lambda pair: (-pair[0], pair[1].title_norm))
        return [track for _, track in scored[:limit]]

    @staticmethod
    def _score(
        track: Track, query: str, tokens: list[str], title: str, artist: str
    ) -> float:
        score = 0.0
        if title == query:
            score += 1000
        elif title.startswith(query):
            score += 400
        elif query in title:
            score += 250
        if artist and query in artist:
            score += 120
        for token in tokens:
            if token in title:
                score += 25
            elif token in artist:
                score += 12
            else:
                score += 3
        if track.lossless:
            score += 8
        # Shorter titles are usually the track, not a folder's full path.
        score -= min(len(title), 200) / 200
        return score

    # ── status ───────────────────────────────────────────────────────────

    def status(self) -> dict:
        counts = {kind: 0 for kind in KINDS}
        for track in self._tracks:
            counts[track.kind] = counts.get(track.kind, 0) + 1
        return {
            "tracks": len(self._tracks),
            "by_kind": counts,
            "last_refresh": self.last_refresh,
            "last_attempt": self.last_attempt,
            "errors": self.errors,
            "refreshing": self._refresh_lock.locked(),
        }
