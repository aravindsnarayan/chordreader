import httpx
import pytest

from app.config import ConfigError, load_settings
from app.torbox import TorBox, TorBoxError, TorBoxRateLimited, TorBoxUnavailable


def test_settings_parse_durations_and_flags(tmp_path):
    settings = load_settings(
        {
            "TORBOX_API_KEY": "key",
            "ADDON_SECRET": "x" * 20,
            "DATA_DIR": str(tmp_path),
            "REFRESH_INTERVAL": "1h",
            "INCLUDE_WEB": "false",
            "AUDIO_EXTENSIONS": ".FLAC, m4a",
        }
    )
    assert settings.refresh_seconds == 3600
    assert settings.include_web is False
    assert settings.audio_extensions == frozenset({"flac", "m4a"})


def test_settings_require_an_api_key(tmp_path):
    with pytest.raises(ConfigError):
        load_settings({"DATA_DIR": str(tmp_path)})


def test_settings_generate_and_reuse_a_secret(tmp_path):
    first = load_settings({"TORBOX_API_KEY": "key", "DATA_DIR": str(tmp_path)})
    second = load_settings({"TORBOX_API_KEY": "key", "DATA_DIR": str(tmp_path)})
    assert first.secret == second.secret
    assert len(first.secret) > 20


def transport(handler):
    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_list_items_paginates():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params.get("offset"))
        if request.url.params.get("offset") == "0":
            return httpx.Response(200, json={"success": True, "data": [{"id": 1}] * 2})
        return httpx.Response(200, json={"success": True, "data": [{"id": 2}]})

    client = TorBox("key", transport=transport(handler))
    assert len(await client.list_items("torrents", page_size=2)) == 3
    assert seen == ["0", "2"]
    await client.close()


@pytest.mark.asyncio
async def test_bad_api_key_is_a_configuration_error():
    client = TorBox("key", transport=transport(lambda request: httpx.Response(401)))
    with pytest.raises(TorBoxError, match="API key"):
        await client.list_items("torrents")
    await client.close()


@pytest.mark.asyncio
async def test_rate_limit_is_reported_with_retry_after():
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) < 3:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"success": True, "data": []})

    client = TorBox("key", transport=transport(handler))
    assert await client.list_items("torrents") == []
    assert len(calls) == 3
    await client.close()


@pytest.mark.asyncio
async def test_always_rate_limited_raises():
    client = TorBox("key", transport=transport(lambda request: httpx.Response(429)))
    with pytest.raises(TorBoxRateLimited):
        await client.list_items("torrents")
    await client.close()


@pytest.mark.asyncio
async def test_server_errors_are_unavailable():
    client = TorBox("key", transport=transport(lambda request: httpx.Response(503)))
    with pytest.raises(TorBoxUnavailable):
        await client.list_items("torrents")
    await client.close()


@pytest.mark.asyncio
async def test_download_link_uses_the_right_parameter_for_each_kind():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url.path), dict(request.url.params)))
        return httpx.Response(200, json={"success": True, "data": "https://cdn/file"})

    client = TorBox("key", transport=transport(handler))
    assert await client.download_link("usenet", 4, 5) == "https://cdn/file"
    assert seen[0][0] == "/v1/api/usenet/requestdl"
    assert seen[0][1]["usenet_id"] == "4"
    assert seen[0][1]["file_id"] == "5"
    assert seen[0][1]["token"] == "key"
    await client.close()
