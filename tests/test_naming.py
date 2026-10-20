import pytest

from app.naming import extension_of, normalize, parse_path


@pytest.mark.parametrize(
    ("path", "title", "artist", "album"),
    [
        ("Artist/Album/01 - Song.flac", "Song", "Artist", "Album"),
        ("Artist - Album/01. Song.flac", "Song", "Artist", "Album"),
        ("Artist - Song.flac", "Song", "Artist", ""),
        ("Artist - Album - 01 - Song.flac", "Song", "Artist", "Album"),
        ("01 - Song.flac", "Song", "", ""),
        ("Song.flac", "Song", "", ""),
        ("Artist/Album/CD1/01 - Song.flac", "Song", "Artist", "Album"),
        ("Artist/Album/1-01 Song.flac", "Song", "Artist", "Album"),
        ("50 Cent - In Da Club.flac", "In Da Club", "50 Cent", ""),
        ("Artist/Album/Song - Live.flac", "Song - Live", "Artist", "Album"),
        ("Artist - Album/Song - feat. X.flac", "Song - feat. X", "Artist", "Album"),
        ("Music/Artist/Album/01 Song.flac", "Song", "Artist", "Album"),
        ("Artist/Album/01 - Mr. Brightside.flac", "Mr. Brightside", "Artist", "Album"),
        (
            "Artist/Album/01 - Song (Official Audio) [FLAC].flac",
            "Song",
            "Artist",
            "Album",
        ),
        ("Artist/Album/01 - Song - Remix.flac", "Song - Remix", "Artist", "Album"),
        ("Artist/Album/Artist - Song.flac", "Song", "Artist", "Album"),
        ("Artist/Album/1999 - Song.flac", "1999 - Song", "Artist", "Album"),
        ("Artist/Album/01 - Song (2020).flac", "Song", "Artist", "Album"),
    ],
)
def test_parse_path(path, title, artist, album):
    parsed = parse_path(path)
    assert (parsed.title, parsed.artist, parsed.album) == (title, artist, album)


def test_parse_path_never_drops_an_unparseable_name():
    assert parse_path("!!!.flac").title == "!!!"


def test_normalize_ignores_case_accents_and_punctuation():
    assert normalize("Björk — Jóga (Live)") == "bjork joga live"


def test_extension_of_handles_folders_and_case():
    assert extension_of("A/B/Song.FLAC") == "flac"
    assert extension_of("Song") == ""
