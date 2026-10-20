from __future__ import annotations

import asyncio
import html
import logging
import secrets
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

from .config import Settings, load_settings
from .library import Library, Track
from .torbox import (
    TorBox,
    TorBoxError,
    TorBoxNoLink,
    TorBoxRateLimited,
    TorBoxUnavailable,
)

log = logging.getLogger("addon")

VERSION = "1.0.0"

CODECS = {
    "flac": "flac",
    "wav": "wav",
    "aiff": "aiff",
    "aif": "aiff",
    "alac": "alac",
    "m4a": "m4a",
    "mp3": "mp3",
    "ogg": "ogg",
    "opus": "opus",
    "aac": "aac",
    "webm": "webm",
}
PASS_HEADERS = {
    "content-type",
    "content-length",
    "content-range",
    "accept-ranges",
    "etag",
    "last-modified",
}


class LinkCache:
    """TorBox download links, reused until shortly before they expire."""

    def __init__(self, ttl: int = 2 * 60 * 60):
        self._ttl = ttl
        self._entries: dict[str, tuple[str, float]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def get(self, track: Track, torbox: TorBox, force: bool = False) -> str:
        now = time.time()
        if not force:
            entry = self._entries.get(track.id)
            if entry and entry[1] > now:
                return entry[0]

        lock = self._locks.setdefault(track.id, asyncio.Lock())
        async with lock:
            if not force:
                entry = self._entries.get(track.id)
                if entry and entry[1] > time.time():
                    return entry[0]
            url = await torbox.download_link(track.kind, track.item_id, track.file_id)
            self._entries[track.id] = (url, time.time() + self._ttl)
            return url

    def drop(self, track_id: str) -> None:
        self._entries.pop(track_id, None)


def _external_base(request: Request, settings: Settings) -> str:
    if settings.public_base_url:
        return settings.public_base_url
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    proto = proto.split(",")[0].strip() or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    host = (host or request.url.netloc).split(",")[0].strip()
    return f"{proto}://{host}".rstrip("/")


def _codec(track: Track) -> str:
    return CODECS.get(track.ext, track.ext)


def _quality(track: Track) -> str:
    return "LOSSLESS" if track.lossless else "HIGH"


def _track_row(track: Track) -> dict:
    row = {
        "id": track.id,
        "title": track.title,
        "artist": track.artist,
        "album": track.album,
        "format": _codec(track),
        "audioQuality": _quality(track),
        "audioMode": "STEREO",
    }
    return {key: value for key, value in row.items() if value != ""}


class ResolveError(Exception):
    def __init__(self, status: int, message: str, headers: dict | None = None):
        super().__init__(message)
        self.status = status
        self.headers = headers or {}


def _error(status: int, message: str, headers: dict | None = None) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status, headers=headers or {})


async def _resolve(torbox: TorBox, track: Track, cache: LinkCache, force: bool) -> str:
    try:
        return await cache.get(track, torbox, force=force)
    except TorBoxRateLimited as exc:
        raise ResolveError(429, str(exc), {"Retry-After": str(exc.retry_after)}) from exc
    except TorBoxUnavailable as exc:
        raise ResolveError(502, str(exc)) from exc
    except TorBoxNoLink as exc:
        raise ResolveError(404, str(exc)) from exc
    except TorBoxError as exc:
        raise ResolveError(502, str(exc)) from exc


async def _close(response: httpx.Response) -> None:
    await response.aclose()


def create_app(
    settings: Settings | None = None,
    torbox: TorBox | None = None,
    client: httpx.AsyncClient | None = None,
) -> FastAPI:
    configured = settings

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal settings, torbox
        settings = configured or load_settings()
        torbox = torbox or TorBox(
            settings.api_key,
            base_url=settings.torbox_base_url,
            timeout=settings.request_timeout,
        )
        library = Library(settings)
        app.state.settings = settings
        app.state.torbox = torbox
        app.state.library = library
        app.state.links = LinkCache()
        app.state.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=15.0),
            follow_redirects=True,
            headers={"User-Agent": "bitchord-torbox-addon/1.0"},
        )

        refresh_task: asyncio.Task | None = None

        async def refresh_forever() -> None:
            try:
                if settings.startup_refresh:
                    await library.refresh(torbox)
                while True:
                    await asyncio.sleep(settings.refresh_seconds)
                    await library.refresh(torbox)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("background refresh stopped")

        if settings.startup_refresh or settings.refresh_seconds > 0:
            refresh_task = asyncio.create_task(refresh_forever())

        yield

        if refresh_task:
            refresh_task.cancel()
            try:
                await refresh_task
            except asyncio.CancelledError:
                pass
        await app.state.client.aclose()
        await torbox.close()

    app = FastAPI(title="BitChord TorBox addon", version=VERSION, lifespan=lifespan)

    def authorised(secret: str) -> bool:
        return bool(secret) and secrets.compare_digest(secret, app.state.settings.secret)

    def guarded(secret: str) -> JSONResponse | None:
        return None if authorised(secret) else _error(404, "Not found")

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True, "version": VERSION}

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse(
            "<!doctype html><meta charset=utf-8><title>BitChord TorBox addon</title>"
            "<body style='font:16px system-ui;max-width:38rem;margin:3rem auto;padding:0 1rem'>"
            "<h1>BitChord TorBox addon</h1>"
            "<p>Running. Open the addon URL (with its secret path) from your "
            "<code>.env</code> to see status and copy the link into BitChord.</p>"
            "<p><a href='/healthz'>health</a></p></body>"
        )

    @app.get("/{secret}/", response_class=HTMLResponse)
    async def status(secret: str, request: Request) -> Response:
        denial = guarded(secret)
        if denial:
            return denial
        library: Library = app.state.library
        info = library.status()
        base = _external_base(request, app.state.settings)
        addon_url = html.escape(f"{base}/{secret}")
        rows = "".join(
            f"<tr><td>{kind}</td><td>{count}</td></tr>"
            for kind, count in info["by_kind"].items()
        )
        errors = "".join(
            f"<li><code>{html.escape(kind)}</code>: {html.escape(message)}</li>"
            for kind, message in info["errors"].items()
        )
        refreshed = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(info["last_refresh"])) if info["last_refresh"] else "never"
        return HTMLResponse(
            "<!doctype html><meta charset=utf-8><title>BitChord TorBox addon</title>"
            "<body style='font:16px system-ui;max-width:44rem;margin:3rem auto;padding:0 1rem'>"
            "<h1>BitChord TorBox addon</h1>"
            f"<p>Paste this into BitChord &rarr; Sources &rarr; Add an addon:</p>"
            f"<p><code style='font-size:1.1rem;word-break:break-all'>{addon_url}</code></p>"
            f"<p>{info['tracks']} tracks, refreshed {refreshed}.</p>"
            f"<table><tbody>{rows}</tbody></table>"
            f"<p>{'Refreshing now.' if info['refreshing'] else ''}</p>"
            f"<ul>{errors}</ul>"
            "<p><a href='./manifest.json'>manifest.json</a></p></body>"
        )

    @app.get("/{secret}/manifest.json")
    async def manifest(secret: str) -> Response:
        denial = guarded(secret)
        if denial:
            return denial
        return JSONResponse(
            {
                "id": "torbox",
                "name": "TorBox",
                "version": VERSION,
                "description": "Music files already in your TorBox account",
                "resources": ["search", "stream"],
                "types": ["track"],
                "allowDownloads": True,
                "checkValidLossless": False,
            }
        )

    @app.get("/{secret}/search")
    async def search(secret: str, q: str = "") -> Response:
        denial = guarded(secret)
        if denial:
            return denial
        library: Library = app.state.library
        tracks = library.search(q)
        return JSONResponse({"tracks": [_track_row(track) for track in tracks]})

    @app.get("/{secret}/stream/{track_id}")
    async def stream(secret: str, track_id: str, request: Request) -> Response:
        denial = guarded(secret)
        if denial:
            return denial
        library: Library = app.state.library
        track = library.get(track_id)
        if track is None:
            return _error(404, "No such track")
        try:
            direct = await _resolve(app.state.torbox, track, app.state.links, force=False)
        except ResolveError as failure:
            return _error(failure.status, str(failure), failure.headers)

        base = _external_base(request, app.state.settings)
        url = (
            direct
            if not app.state.settings.proxy_audio
            else f"{base}/{secret}/audio/{track.id}"
        )
        codec = _codec(track)
        return JSONResponse(
            {
                "url": url,
                "format": codec,
                "quality": _quality(track),
                "codec": codec,
                "container": "mp4" if track.ext in {"m4a", "aac"} else codec,
                "manifest": "none",
                "encrypted": False,
                "audioMode": "STEREO",
                "size": track.size,
            }
        )

    async def proxy(track: Track, request: Request) -> Response:
        library: Library = app.state.library
        if library.get(track.id) is None:
            return _error(404, "No such track")

        headers: dict[str, str] = {}
        if request.headers.get("range"):
            headers["Range"] = request.headers["range"]
        if request.headers.get("if-range"):
            headers["If-Range"] = request.headers["if-range"]

        client: httpx.AsyncClient = app.state.client
        response: httpx.Response | None = None
        for attempt in range(2):
            try:
                url = await _resolve(
                    app.state.torbox, track, app.state.links, force=attempt > 0
                )
            except ResolveError as failure:
                return _error(failure.status, str(failure), failure.headers)

            upstream = client.build_request(
                "GET",
                url,
                headers=headers,
                timeout=httpx.Timeout(60.0, connect=15.0),
            )
            try:
                response = await client.send(upstream, stream=True)
            except httpx.HTTPError as exc:
                log.warning("CDN request failed for %s (%s)", track.id, type(exc).__name__)
                return _error(502, "Could not reach the TorBox download link")

            if response.status_code in (401, 403, 404, 410) and attempt == 0:
                await response.aclose()
                app.state.links.drop(track.id)
                continue
            break

        assert response is not None
        passthrough = {
            key: value
            for key, value in response.headers.items()
            if key.lower() in PASS_HEADERS
        }
        passthrough.setdefault("accept-ranges", "bytes")
        passthrough.setdefault("content-type", "application/octet-stream")
        return StreamingResponse(
            response.aiter_raw(),
            status_code=response.status_code,
            headers=passthrough,
            background=BackgroundTask(_close, response),
        )

    @app.api_route("/{secret}/audio/{track_id}", methods=["GET", "HEAD"])
    async def audio(secret: str, track_id: str, request: Request) -> Response:
        denial = guarded(secret)
        if denial:
            return denial
        library: Library = app.state.library
        track = library.get(track_id)
        if track is None:
            return _error(404, "No such track")
        if request.method == "HEAD":
            # ExoPlayer probes before it ranges. Resolve the link so a missing
            # file fails now, and answer with the size TorBox listed.
            try:
                await _resolve(app.state.torbox, track, app.state.links, force=False)
            except ResolveError as failure:
                return _error(failure.status, str(failure), failure.headers)
            content_type = {
                "mp3": "audio/mpeg",
                "m4a": "audio/mp4",
                "aac": "audio/aac",
                "flac": "audio/flac",
                "wav": "audio/wav",
                "aiff": "audio/aiff",
                "ogg": "audio/ogg",
                "opus": "audio/ogg",
            }.get(_codec(track), "application/octet-stream")
            return Response(
                status_code=200,
                headers={
                    "accept-ranges": "bytes",
                    "content-length": str(track.size),
                    "content-type": content_type,
                },
            )
        return await proxy(track, request)

    @app.post("/{secret}/refresh")
    async def refresh(secret: str) -> Response:
        denial = guarded(secret)
        if denial:
            return denial
        library: Library = app.state.library
        await library.refresh(app.state.torbox)
        return JSONResponse(library.status())

    @app.get("/{secret}/status")
    async def status_json(secret: str) -> Response:
        denial = guarded(secret)
        if denial:
            return denial
        return JSONResponse(app.state.library.status())

    return app


# uvicorn app.main:app
app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8080)
