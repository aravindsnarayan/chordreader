from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.torbox import TorBoxNoLink

SECRET = "test-secret-1234567890"


def settings(tmp_path: Path, **overrides) -> Settings:
    values = dict(
        api_key="key",
        secret=SECRET,
        data_dir=tmp_path,
        startup_refresh=False,
        refresh_seconds=0,
    )
    values.update(overrides)
    return Settings(**values)


def item(item_id: int, name: str, files: list[dict]) -> dict:
    return {"id": item_id, "name": name, "download_finished": True, "files": files}


class FakeTorBox:
    def __init__(self) -> None:
        self.links: list[tuple[str, int, int]] = []

    async def list_items(self, kind, page_size=1000, max_pages=50):
        if kind != "torrents":
            return []
        return [
            item(
                7,
                "Test Artist - Test Album",
                [
                    {
                        "id": 3,
                        "name": "Test Artist - Test Album/01 - Test Song.flac",
                        "size": 100,
                    }
                ],
            )
        ]

    async def download_link(self, kind, item_id, file_id):
        self.links.append((kind, item_id, file_id))
        return f"https://cdn.test/{kind}/{item_id}/{file_id}"

    async def close(self):
        pass


def cdn(request: httpx.Request) -> httpx.Response:
    body = bytes(range(100))

    class Body(httpx.AsyncByteStream):
        def __init__(self, data: bytes):
            self.data = data

        async def __aiter__(self):
            yield self.data

    range_header = request.headers.get("range")
    if range_header == "bytes=0-9":
        return httpx.Response(
            206,
            stream=Body(body[:10]),
            headers={
                "content-type": "audio/flac",
                "content-range": "bytes 0-9/100",
                "accept-ranges": "bytes",
            },
        )
    return httpx.Response(
        200,
        stream=Body(body),
        headers={"content-type": "audio/flac", "accept-ranges": "bytes"},
    )


def client_for(tmp_path: Path, **overrides) -> TestClient:
    app = create_app(
        settings(tmp_path, **overrides),
        FakeTorBox(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(cdn)),
    )
    return TestClient(app)


def test_manifest_and_search(tmp_path):
    with client_for(tmp_path) as client:
        assert client.get("/healthz").json()["ok"] is True

        assert client.get(f"/{SECRET}/manifest.json").status_code == 200
        assert client.get("/wrong/manifest.json").status_code == 404

        client.post(f"/{SECRET}/refresh")
        body = client.get(f"/{SECRET}/search", params={"q": "test song"}).json()

        assert body["tracks"] == [
            {
                "id": "t-7-3",
                "title": "Test Song",
                "artist": "Test Artist",
                "album": "Test Album",
                "format": "flac",
                "audioQuality": "LOSSLESS",
                "audioMode": "STEREO",
            }
        ]


def test_stream_returns_a_proxy_url_and_audio_supports_ranges(tmp_path):
    with client_for(tmp_path) as client:
        client.post(f"/{SECRET}/refresh")
        stream = client.get(f"/{SECRET}/stream/t-7-3")

        assert stream.status_code == 200
        body = stream.json()
        assert body["url"] == f"http://testserver/{SECRET}/audio/t-7-3"
        assert body["codec"] == "flac"
        assert body["quality"] == "LOSSLESS"

        audio = client.get(f"/{SECRET}/audio/t-7-3", headers={"Range": "bytes=0-9"})
        assert audio.status_code == 206
        assert audio.headers["content-range"] == "bytes 0-9/100"
        assert audio.content == bytes(range(10))


def test_stream_can_hand_out_the_cdn_url_directly(tmp_path):
    with client_for(tmp_path, proxy_audio=False) as client:
        client.post(f"/{SECRET}/refresh")
        body = client.get(f"/{SECRET}/stream/t-7-3").json()
        assert body["url"] == "https://cdn.test/torrents/7/3"


def test_unknown_track_and_wrong_secret_are_404(tmp_path):
    with client_for(tmp_path) as client:
        client.post(f"/{SECRET}/refresh")
        assert client.get(f"/{SECRET}/stream/t-1-1").status_code == 404
        assert client.get(f"/{SECRET}/audio/t-1-1").status_code == 404
        assert client.get(f"/{SECRET}/search?q=x", headers={"Host": "example"}).status_code == 200
        assert client.get("/other/stream/t-7-3").status_code == 404


def test_missing_torbox_link_is_404(tmp_path):
    class NoLinkTorBox(FakeTorBox):
        async def download_link(self, kind, item_id, file_id):
            raise TorBoxNoLink("no playable link")

    app = create_app(
        settings(tmp_path),
        NoLinkTorBox(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(cdn)),
    )
    with TestClient(app) as client:
        client.post(f"/{SECRET}/refresh")
        assert client.get(f"/{SECRET}/stream/t-7-3").status_code == 404
        assert client.get(f"/{SECRET}/audio/t-7-3").status_code == 404


def test_head_reports_the_size_without_fetching_the_file(tmp_path):
    with client_for(tmp_path) as client:
        client.post(f"/{SECRET}/refresh")
        response = client.head(f"/{SECRET}/audio/t-7-3")
        assert response.status_code == 200
        assert response.headers["content-length"] == "100"
        assert response.headers["accept-ranges"] == "bytes"
