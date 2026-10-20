from pathlib import Path

import pytest

from app.config import Settings
from app.library import Library, Track, parse_track_id
from app.torbox import TorBoxError


def settings(tmp_path: Path, **overrides) -> Settings:
    values = dict(
        api_key="key",
        secret="a" * 24,
        data_dir=tmp_path,
        startup_refresh=False,
        refresh_seconds=0,
    )
    values.update(overrides)
    return Settings(**values)


def item(item_id: int, name: str, *files: tuple[int, str, int]) -> dict:
    return {
        "id": item_id,
        "name": name,
        "download_finished": True,
        "files": [{"id": file_id, "name": path, "size": size} for file_id, path, size in files],
    }


class FakeTorBox:
    def __init__(self, pages: dict[str, list[dict]]):
        self.pages = pages
        self.calls: list[str] = []

    async def list_items(self, kind, page_size=1000, max_pages=50):
        self.calls.append(kind)
        return self.pages.get(kind, [])


@pytest.mark.asyncio
async def test_refresh_indexes_audio_and_skips_everything_else(tmp_path):
    library = Library(settings(tmp_path))
    torbox = FakeTorBox(
        {
            "torrents": [
                item(
                    1,
                    "Artist - Album",
                    (0, "Artist - Album/01 - Song.flac", 100),
                    (1, "Artist - Album/cover.jpg", 10),
                    (2, "Artist - Album/02 - Other.mp3", 50),
                )
            ]
        }
    )

    await library.refresh(torbox)

    assert [track.title for track in library.tracks] == ["Song"]
    assert library.get("t-1-0").artist == "Artist"
    assert library.get("t-1-0").album == "Album"
    assert library.get("t-1-0").lossless is True
    assert library.get("t-1-1") is None
    assert torbox.calls == ["torrents", "usenet", "webdl"]


@pytest.mark.asyncio
async def test_audio_extensions_can_include_lossy_files(tmp_path):
    library = Library(settings(tmp_path, audio_extensions=frozenset({"flac", "mp3"})))
    await library.refresh(
        FakeTorBox(
            {"torrents": [item(1, "A", (0, "A/01 - One.mp3", 10), (1, "A/02 - Two.flac", 10))]}
        )
    )
    assert library.get("t-1-0").lossless is False
    assert library.get("t-1-1").lossless is True


@pytest.mark.asyncio
async def test_release_folder_supplies_a_missing_artist(tmp_path):
    library = Library(settings(tmp_path))
    await library.refresh(
        FakeTorBox(
            {
                "torrents": [
                    item(
                        1,
                        "Test Artist - Test Album [FLAC]",
                        (0, "Test Album/01 - Test Song.flac", 10),
                    )
                ]
            }
        )
    )
    track = library.get("t-1-0")
    assert (track.title, track.artist, track.album) == ("Test Song", "Test Artist", "Test Album")


@pytest.mark.asyncio
async def test_refresh_keeps_old_rows_when_one_collection_fails(tmp_path):
    library = Library(settings(tmp_path))
    good = FakeTorBox({"torrents": [item(1, "A", (0, "A/01 - One.flac", 10))]})
    await library.refresh(good)

    class Partial(FakeTorBox):
        async def list_items(self, kind, page_size=1000, max_pages=50):
            if kind == "torrents":
                raise TorBoxError("down")
            return await super().list_items(kind, page_size, max_pages)

    await library.refresh(Partial({"usenet": [item(2, "B", (0, "B/01 - Two.flac", 10))]}))

    assert {track.title for track in library.tracks} == {"One", "Two"}
    assert library.errors == {"torrents": "down"}


@pytest.mark.asyncio
async def test_refresh_keeps_everything_when_all_collections_fail(tmp_path):
    library = Library(settings(tmp_path))
    await library.refresh(FakeTorBox({"torrents": [item(1, "A", (0, "A/01 - One.flac", 10))]}))

    class Down(FakeTorBox):
        async def list_items(self, kind, page_size=1000, max_pages=50):
            raise TorBoxError("down")

    await library.refresh(Down({}))

    assert [track.title for track in library.tracks] == ["One"]
    assert library.errors


@pytest.mark.asyncio
async def test_index_survives_a_restart(tmp_path):
    first = Library(settings(tmp_path))
    await first.refresh(FakeTorBox({"torrents": [item(1, "A", (0, "A/01 - One.flac", 10))]}))
    first._save()

    second = Library(settings(tmp_path))
    assert [track.title for track in second.tracks] == ["One"]


def test_search_ranks_title_before_path(tmp_path):
    library = Library(settings(tmp_path))
    library._replace(
        [
            Track(
                id="t-1-0",
                kind="torrents",
                item_id=1,
                file_id=0,
                path="Various/Compilation/01 - Time.flac",
                title="Time",
                artist="Artist",
                album="Compilation",
                ext="flac",
                size=10,
            ),
            Track(
                id="t-1-1",
                kind="torrents",
                item_id=1,
                file_id=1,
                path="A/01 - Timebomb.flac",
                title="Timebomb",
                artist="Artist",
                album="A",
                ext="flac",
                size=10,
            ),
        ]
    )
    assert [track.title for track in library.search("time artist")] == ["Time", "Timebomb"]


def test_search_returns_nothing_for_an_empty_query(tmp_path):
    assert Library(settings(tmp_path)).search("  ") == []


@pytest.mark.parametrize(
    ("track_id", "expected"),
    [
        ("t-12-3", ("torrents", 12, 3)),
        ("u-4-5", ("usenet", 4, 5)),
        ("w-6-7", ("webdl", 6, 7)),
        ("x-1-2", None),
        ("t-1", None),
        ("t-a-b", None),
    ],
)
def test_parse_track_id(track_id, expected):
    assert parse_track_id(track_id) == expected
