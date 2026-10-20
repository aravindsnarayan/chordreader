# BitChord TorBox addon

A self-hosted [BitChord](https://github.com/kushagrasinghx/BitChord) addon
server that turns the music files already in your TorBox account into a
BitChord source. Point BitChord at this server in **Sources → Add an addon** and
your FLAC/WAV/AIFF files play through the same player, with the original file
untouched.

It reads TorBox's torrents, usenet downloads and web downloads. It does not
search torrents, add content, or host anything: the files have to be in your
account already.

## What it does

- Indexes every audio file in your TorBox account and keeps the index on disk.
- Answers the BitChord addon protocol: `/manifest.json`, `/search?q=…`,
  `/stream/{id}`.
- Streams through the addon with HTTP Range support, so seeking works and the
  TorBox API key never reaches the phone.
- Reports FLAC, WAV, AIFF and ALAC as lossless, with codec, container and
  quality metadata BitChord uses to rank sources.
- Refreshes the index in the background, and keeps the old rows if TorBox has
  a bad minute.
- Generates an addon secret on first start if you do not set one.

## Requirements

- A TorBox account and API key (`https://torbox.app/settings`).
- Docker with Compose.
- HTTPS in front of the addon. BitChord talks to addons over HTTPS; use Caddy,
  nginx, Traefik or a Cloudflare Tunnel. Plain HTTP is fine for local tests
  with a client that allows it.

## Setup

```bash
cp .env.example .env
```

Edit `.env`:

```dotenv
TORBOX_API_KEY=your-torbox-api-key
ADDON_SECRET=replace-with-a-long-random-string
```

Generate the secret with:

```bash
openssl rand -hex 24
```

Use URL-safe characters only (letters, digits, `.`, `_`, `~`, `-`). Spaces and
`/` break the addon URL. A generated secret is always safe.

Start it:

```bash
docker compose up -d --build
```

Open `https://your-host/<secret>/`. The page shows the exact URL to paste:

```
https://your-host/<secret>
```

In BitChord: **Sources → Add an addon**, paste the URL, and press Test. The
first index starts immediately; a large account can take a minute or two. Search
works from the saved index while a refresh is running.

If you leave `ADDON_SECRET` empty, the addon generates one on first start,
saves it in the `torbox-data` volume, and prints it to the container log:

```bash
docker compose logs torbox-addon | grep ADDON_SECRET
# or
docker compose exec torbox-addon cat /data/secret
```

## Reverse proxy

The only special requirement is that the proxy does not buffer audio
responses. Seeking and track changes depend on Range requests reaching the
addon immediately.

Caddy:

```caddyfile
music.example.com {
    reverse_proxy 127.0.0.1:8080 {
        flush_interval -1
    }
}
```

nginx:

```nginx
location / {
    proxy_pass http://127.0.0.1:8080;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_buffering off;
    proxy_request_buffering off;
    proxy_read_timeout 1h;
}
```

Cloudflare Tunnel:

```bash
cloudflared tunnel --url http://localhost:8080
```

Use the resulting HTTPS hostname plus `/<secret>` as the addon URL. If your
proxy does not send `X-Forwarded-Proto` or `X-Forwarded-Host`, set
`PUBLIC_BASE_URL=https://music.example.com` in `.env` so stream URLs are built
with the right origin.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `TORBOX_API_KEY` | required | TorBox API key. |
| `ADDON_SECRET` | generated | Path secret. The addon URL is `https://host/<secret>`. Treat it like a password. |
| `PUBLIC_BASE_URL` | derived from request | Public origin, e.g. `https://music.example.com`. |
| `REFRESH_INTERVAL` | `15m` | How often to re-read the account. `900s`, `15m`, `1h` all work. |
| `AUDIO_EXTENSIONS` | `flac,wav,aiff,aif,alac,m4a` | Comma-separated extensions to index. Add `mp3,ogg,opus` if you want lossy files too. |
| `INCLUDE_TORRENTS` | `true` | Read the torrents collection. |
| `INCLUDE_USENET` | `true` | Read the usenet collection. |
| `INCLUDE_WEB` | `true` | Read the web downloads collection. |
| `PROXY_AUDIO` | `true` | Stream through the addon. `false` hands BitChord the TorBox CDN URL instead. |
| `STARTUP_REFRESH` | `true` | Refresh as soon as the container starts. |
| `PAGE_SIZE` | `1000` | TorBox list page size (max 1000). |
| `MAX_PAGES` | `50` | Safety cap on pages per collection. |
| `REQUEST_TIMEOUT` | `30` | TorBox API timeout in seconds. |

## How search works

TorBox stores original file names; it does not expose music tags. The addon
parses the path into title, artist and album. These all work:

```
Artist/Album/01 - Title.flac
Artist - Album/01. Title.flac
Artist - Title.flac
Artist - Album - 01 - Title.flac
01 - Title.flac                 (artist and album from the folders)
```

Leading track numbers, `CD1`/`Disc 1` folders, quality tags like `[FLAC]`, and
trailing years are removed. Underscores become spaces. If a name cannot be
parsed, the filename is still used as the title, so the file is not lost.

For the best matching in BitChord, name files like `Artist - Title.flac` or
keep the usual `Artist/Album/01 - Title.flac` layout. Embedded tags are not
read.

A few deliberate limits:

- **No duration or artwork.** BitChord matches on title, artist and album.
- **`.m4a` is reported as `m4a`, not necessarily ALAC.** If you want the
  lossless label, use `.flac`, `.wav`, `.aiff` or `.alac`.
- **Duplicate releases are collapsed** when title, artist, album and size all
  match, so ten copies of the same album do not fill the result list.

## Security

- The TorBox API key stays in the container. It is never included in the
  manifest, search results or stream URLs.
- The path secret is the only credential BitChord uses. Anyone with the full
  addon URL can stream your library; rotate `ADDON_SECRET` and restart to
  revoke every old link.
- The addon runs as an unprivileged user and only needs outbound HTTPS to
  TorBox, plus the inbound port your proxy uses.
- `/` and `/healthz` expose health only. The status page with the addon URL is
  under the secret path.

## Troubleshooting

| Symptom | What to do |
| --- | --- |
| BitChord says "No manifest at that URL" | Check the URL includes `/<secret>/manifest.json` implicitly; the root should be `https://host/<secret>`. |
| `401`/`403` in the addon log | The TorBox API key is wrong or revoked. Update `.env` and restart. |
| 0 tracks after refresh | Check `AUDIO_EXTENSIONS`, and whether the files are completed in TorBox. Unfinished downloads are skipped. |
| Search finds tracks, playback fails | The proxy is buffering or timing out. See the reverse proxy section. |
| Playback works, seeking does not | Range requests are not reaching the addon. Turn off proxy buffering. |
| New files do not appear | Wait for `REFRESH_INTERVAL`, or `POST https://host/<secret>/refresh`. |
| TorBox rate limit | The addon reuses download links and only asks once per track per two hours. Lower `PAGE_SIZE` or raise `REFRESH_INTERVAL` if you share the key. |
| Container cannot write `/data` | The named volume is owned by the container user. If you switch to a bind mount, `chown 65532` the host folder. |

## Development

```bash
docker run --rm -v "$PWD":/app -w /app python:3.12-slim \
  sh -c "pip install -r requirements-dev.txt && pytest -q"
```

Run locally without Docker:

```bash
pip install -r requirements-dev.txt
DATA_DIR=./data TORBOX_API_KEY=... ADDON_SECRET=... \
  uvicorn app.main:app --reload --port 8080
```

The protocol endpoints are:

| Endpoint | Purpose |
| --- | --- |
| `GET /<secret>/manifest.json` | Addon identity and capabilities. |
| `GET /<secret>/search?q=…` | Track rows (`tracks[]`). |
| `GET /<secret>/stream/{id}` | JSON with a playable `url` and codec metadata. |
| `GET /<secret>/audio/{id}` | Proxied audio, supports `Range` and `HEAD`. |
| `POST /<secret>/refresh` | Refresh now. |
| `GET /<secret>/status` | Index counts, last refresh and per-collection errors. |
| `GET /healthz` | Liveness for Docker. |

## Legal

This addon only exposes files that are already in your own TorBox account. It
does not search for, add, or host copyrighted content. You are responsible for
what you store and stream, and for complying with TorBox's terms and your local
law.
