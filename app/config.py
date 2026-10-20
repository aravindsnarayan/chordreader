from __future__ import annotations

import os
import re
import secrets
import sys
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Settings:
    api_key: str
    secret: str
    data_dir: Path
    public_base_url: str = ""
    refresh_seconds: int = 900
    audio_extensions: frozenset[str] = field(
        default_factory=lambda: frozenset({"flac", "wav", "aiff", "aif", "alac", "m4a"})
    )
    include_torrents: bool = True
    include_usenet: bool = True
    include_web: bool = True
    proxy_audio: bool = True
    page_size: int = 1000
    max_pages: int = 50
    startup_refresh: bool = True
    torbox_base_url: str = "https://api.torbox.app"
    request_timeout: float = 30.0

    @property
    def kinds(self) -> tuple[str, ...]:
        kinds = []
        if self.include_torrents:
            kinds.append("torrents")
        if self.include_usenet:
            kinds.append("usenet")
        if self.include_web:
            kinds.append("webdl")
        return tuple(kinds)


def _flag(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def _duration(value: str | None, default: int) -> int:
    if not value:
        return default
    match = re.fullmatch(r"\s*(\d+)\s*([smhd]?)\s*", value.lower())
    if not match:
        raise ConfigError(f"Invalid duration: {value!r}")
    number = int(match.group(1))
    unit = match.group(2) or "s"
    return number * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


def _secret(data_dir: Path, value: str | None) -> str:
    value = (value or "").strip().strip("/")
    if value and not value.startswith("change-me"):
        if len(value) < 16:
            print("warning: ADDON_SECRET is shorter than 16 characters", file=sys.stderr)
        return value

    # Zero-config start: keep a generated secret on the data volume so the
    # addon URL survives restarts. It is printed once so it can be copied.
    path = data_dir / "secret"
    if path.is_file():
        return path.read_text(encoding="utf-8").strip()
    value = secrets.token_urlsafe(24)
    data_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    path.chmod(0o600)
    print(
        f"warning: generated ADDON_SECRET; addon URL path is /{value} "
        f"(also saved to {path})",
        file=sys.stderr,
    )
    return value


def load_settings(env: dict[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    api_key = (env.get("TORBOX_API_KEY") or "").strip()
    if not api_key:
        raise ConfigError("TORBOX_API_KEY is required")

    data_dir = Path(env.get("DATA_DIR") or "/data")
    extensions = frozenset(
        ext.strip().lower().lstrip(".")
        for ext in (env.get("AUDIO_EXTENSIONS") or "flac,wav,aiff,aif,alac,m4a").split(",")
        if ext.strip()
    )
    if not extensions:
        raise ConfigError("AUDIO_EXTENSIONS cannot be empty")

    return Settings(
        api_key=api_key,
        secret=_secret(data_dir, env.get("ADDON_SECRET")),
        data_dir=data_dir,
        public_base_url=(env.get("PUBLIC_BASE_URL") or "").strip().rstrip("/"),
        refresh_seconds=_duration(env.get("REFRESH_INTERVAL"), 900),
        audio_extensions=extensions,
        include_torrents=_flag(env.get("INCLUDE_TORRENTS"), True),
        include_usenet=_flag(env.get("INCLUDE_USENET"), True),
        include_web=_flag(env.get("INCLUDE_WEB"), True),
        proxy_audio=_flag(env.get("PROXY_AUDIO"), True),
        page_size=max(1, min(1000, int(env.get("PAGE_SIZE") or 1000))),
        max_pages=max(1, int(env.get("MAX_PAGES") or 50)),
        startup_refresh=_flag(env.get("STARTUP_REFRESH"), True),
        torbox_base_url=(env.get("TORBOX_BASE_URL") or "https://api.torbox.app").rstrip("/"),
        request_timeout=float(env.get("REQUEST_TIMEOUT") or 30),
    )
