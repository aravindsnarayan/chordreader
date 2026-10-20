"""Turn a TorBox file path into the title/artist/album BitChord matches on.

TorBox stores files by the name they had in the torrent, usenet post or web
download. There are no tags to read, so the path is the metadata. The heuristics
here cover the layouts that actually show up in music releases:

    Artist/Album/01 - Title.flac
    Artist - Album/01. Title.flac
    Artist - Title.flac
    Artist - Album - 01 - Title.flac
    01 - Title.flac                  (artist/album from the folders)

Anything that cannot be parsed still becomes a track: the filename is better
than dropping a file the listener owns.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

LOSSLESS_EXTENSIONS = frozenset({"flac", "wav", "aiff", "aif", "alac", "ape", "wv"})
AUDIO_EXTENSIONS = LOSSLESS_EXTENSIONS | {"m4a", "mp3", "ogg", "opus", "aac", "webm"}

# `01 - `, `01. `, `[01] `, `CD1 - 01 - `. Not `50 Cent - ...`: a bare number
# only counts when a separator follows it.
_TRACK_PREFIX = re.compile(
    r"""^\s*
    (?:(?:cd|disc|disk)\s*\d{1,2}\s*[-_. ]*)?
    (?:
        \[\d{1,3}\]\s*
      | \(\d{1,3}\)\s*
      | \d{1,2}-\d{1,3}\s*[-.)\]]\s*
      | \d{1,3}\s*[-.)\]]\s*
      | \d{1,3}\s+[-–—]\s+
      | 0\d{1,2}\s+
    )""",
    re.IGNORECASE | re.VERBOSE,
)

# Packaging, not identity. A title keeps "(Live)", "(Remastered)" and
# "(feat. ...)" because BitChord treats those as version evidence.
_PACKAGING = re.compile(
    r"""\s*[\[(]
    (?:flac|alac|wav|aiff|mp3|aac|m4a|320|256|192|128|lossless|hi[- ]?res|
       \d{1,2}[- ]?bit|\d{2,3}\s*kbps|web[- ]?dl|cd rip|vinyl|official\ audio|
       official\ video|lyric\ video|visuali[sz]er|audio\ only)
    [\])]
    """,
    re.IGNORECASE | re.VERBOSE,
)

_HYPHEN = re.compile(r"\s+[-–—]\s+")
_BRACKETED_TAIL = re.compile(r"\s*[\[(][^\])]*[\])]\s*$")
_YEAR = re.compile(r"\s*[\[(]?(?:19|20)\d{2}[\])]?\s*$")
_DISC_FOLDER = re.compile(r"^(?:cd|disc|disk|volume|vol)\s*\d+$", re.IGNORECASE)

_GENERIC_FOLDERS = {
    "music",
    "audio",
    "downloads",
    "download",
    "complete",
    "completed",
    "incomplete",
    "torrents",
    "torrent",
    "usenet",
    "web downloads",
    "webdl",
    "media",
    "library",
    "libraries",
    "tracks",
    "track",
    "various artists",
    "various",
    "unknown artist",
}

_VERSION_WORDS = {
    "live",
    "remix",
    "acoustic",
    "demo",
    "edit",
    "instrumental",
    "version",
    "mix",
    "reprise",
    "unplugged",
    "extended",
}


@dataclass(frozen=True)
class ParsedName:
    title: str
    artist: str = ""
    album: str = ""


def normalize(text: str) -> str:
    """Lowercase, strip accents and punctuation, collapse whitespace.

    Scripts that do not use spaces (CJK) survive as one token, which is what
    matching on a substring needs.
    """
    folded = unicodedata.normalize("NFKD", text.casefold())
    kept = "".join(
        ch if unicodedata.category(ch)[0] in "LN" else " "
        for ch in folded
        if not unicodedata.combining(ch)
    )
    return " ".join(kept.split())


def _clean(text: str) -> str:
    text = text.replace("_", " ")
    text = _PACKAGING.sub(" ", text)
    text = " ".join(text.split())
    return text.strip(" .-–—")


def _strip_track_number(text: str) -> str:
    previous = None
    while previous != text:
        previous = text
        text = _TRACK_PREFIX.sub("", text, count=1).strip()
    return text


def _split(text: str) -> list[str]:
    return [part.strip() for part in _HYPHEN.split(text) if part.strip()]


def _folder_hint(folders: list[str]) -> tuple[str, str]:
    """Artist and album from the directory names, when they carry them."""
    useful = [
        folder
        for folder in folders
        if normalize(folder) not in _GENERIC_FOLDERS and not _DISC_FOLDER.match(folder)
    ]
    if not useful:
        return "", ""

    last = useful[-1]
    if _DISC_FOLDER.match(last) and len(useful) > 1:
        last = useful[-2]

    pieces = _split(last)
    if len(pieces) >= 2:
        return pieces[0], _YEAR.sub("", " - ".join(pieces[1:])).strip()

    # `Artist/Album/...` is the common shape: the last folder is the album and
    # the one before it is the artist.
    if len(useful) >= 2:
        return useful[-2], _YEAR.sub("", last).strip()

    # A single folder is more often the artist than the album.
    return _YEAR.sub("", last).strip(), ""


def folder_hint(name: str) -> tuple[str, str]:
    """Artist and album a release folder name carries, if any."""
    return _folder_hint([_clean(name)])


def _looks_like_version(text: str) -> bool:
    first = re.split(r"[\s(\[]", text.strip().lower(), maxsplit=1)[0]
    return first in _VERSION_WORDS


def parse_path(path: str) -> ParsedName:
    """Parse one TorBox file name (with or without its folders)."""
    parts = [part for part in re.split(r"[\\/]+", path) if part.strip()]
    if not parts:
        return ParsedName(title="")

    filename = parts[-1]
    folders = [_clean(part) for part in parts[:-1]]
    artist, album = _folder_hint(folders)

    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    stem = _clean(stem)
    stem = _strip_track_number(stem)
    pieces = _split(stem)

    # The folder already told us who the artist is; a repeated artist in the
    # filename is noise, not a second artist.
    if artist and pieces and normalize(pieces[0]) == normalize(artist):
        pieces = pieces[1:]
    if album and pieces and normalize(pieces[0]) == normalize(album):
        pieces = pieces[1:]

    title = ""
    if not pieces:
        title = stem
    elif len(pieces) == 1:
        title = pieces[0]
    elif _looks_like_version(pieces[-1]):
        # `Title - Live`, or `Artist - Title - Live`.
        title = " - ".join(pieces[1:] if not artist and len(pieces) > 2 else pieces)
        artist = artist or pieces[0]
    elif artist and len(pieces) == 2:
        # `Album - Title` under an artist folder, or a title with a dash in it.
        if album:
            title = " - ".join(pieces)
        else:
            album, title = pieces
    elif artist:
        if album and normalize(pieces[0]) == normalize(album):
            title = " - ".join(pieces[1:])
        elif album:
            title = " - ".join(pieces)
        else:
            album, title = pieces[0], " - ".join(pieces[1:])
    elif len(pieces) == 2:
        artist, title = pieces
    else:
        # `Artist - Album - Title` (and with a track number still in the middle).
        artist, album, title = pieces[0], album or pieces[1], " - ".join(pieces[2:])

    title = _strip_track_number(_clean(title))
    if not title:
        title = _strip_track_number(stem)
    title = _YEAR.sub("", title).strip()

    return ParsedName(
        title=title,
        artist=_clean(artist),
        album=_YEAR.sub("", _clean(album)).strip(),
    )


def extension_of(path: str) -> str:
    name = path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""
