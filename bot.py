"""
Highrise DJ Bot - YouTube music streamed to an Icecast/Zeno mount.

Commands (in room chat):
    !play <youtube url or search query>
    !skip
    !stop
    !queue

Pipeline:
    1. yt-dlp resolves a track and gives us a direct audio URL.
    2. ffmpeg pulls that audio URL in real-time and pushes a 128 kbps
       MP3 stream to the Icecast mount.
    3. When ffmpeg exits we advance to the next queued track.

Environment variables (Railway style):
    HIGHRISE_TOKEN   or  HIGHRISE_BOT_TOKEN  — bot API token
    ROOM_ID          or  HIGHRISE_ROOM_ID    — target room ID
    ICECAST_HOST     — Icecast server hostname
    ICECAST_PORT     — Icecast server port (default 80)
    ICECAST_PASSWORD — source password
    ICECAST_MOUNT    — mount path, e.g. /stream
    ICECAST_USER     — source username (default: source)

    Alternatively, set ZENO_SOURCE_URL=icecast://user:pass@host:port/mount
    to configure Icecast in a single URL (overrides individual vars above).
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse, unquote

from highrise import BaseBot, User

try:
    from highrise.models import SessionMetadata
except ImportError:
    try:
        from highrise import SessionMetadata  # type: ignore[attr-defined,no-redef]
    except ImportError:
        SessionMetadata = object  # type: ignore[assignment,misc]

try:
    from highrise.__main__ import BotDefinition, main as highrise_main
except ImportError:
    from highrise import BotDefinition, main as highrise_main  # type: ignore[attr-defined,no-redef]

from yt_dlp import YoutubeDL


YDL_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
    "noplaylist": True,
    "default_search": "ytsearch1",
    "format": "bestaudio/best",
    "extract_flat": False,
}


@dataclass
class Track:
    title: str
    page_url: str
    audio_url: str
    duration: int
    requested_by: str

    def display(self) -> str:
        mins, secs = divmod(self.duration, 60)
        return f"{self.title} [{mins}:{secs:02d}]"


@dataclass
class Player:
    queue: list[Track] = field(default_factory=list)
    current: Optional[Track] = None
    ffmpeg: Optional[asyncio.subprocess.Process] = None
    curl: Optional[asyncio.subprocess.Process] = None
    play_task: Optional[asyncio.Task] = None
    stop_requested: bool = False


@dataclass
class IcecastTarget:
    user: str
    password: str
    host: str
    port: int
    mount: str

    @property
    def http_url(self) -> str:
        return f"http://{self.host}:{self.port}{self.mount}"


def _safe_header(value: str) -> str:
    return re.sub(r"[\r\n]", " ", value)


def parse_icecast_url(url: str) -> IcecastTarget:
    parsed = urlparse(url)
    if parsed.scheme not in ("icecast", "http", "https"):
        raise ValueError(f"Unsupported source URL scheme: {parsed.scheme!r}")
    if not parsed.hostname:
        raise ValueError("Source URL is missing a host")
    user = unquote(parsed.username or "source")
    password = unquote(parsed.password or "")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    mount = parsed.path or "/"
    if not mount.startswith("/"):
        mount = "/" + mount
    return IcecastTarget(user=user, password=password,
                         host=parsed.hostname, port=port, mount=mount)


def build_target_from_env() -> IcecastTarget:
    """Build an IcecastTarget from environment variables.

    Precedence:
    1. ZENO_SOURCE_URL  (single icecast:// URL)
    2. Individual vars: ICECAST_HOST, ICECAST_PORT, ICECAST_PASSWORD,
                        ICECAST_MOUNT, ICECAST_USER
    """
    source_url = os.environ.get("ZENO_SOURCE_URL", "").strip()
    if source_url:
        return parse_icecast_url(source_url)

    host = os.environ.get("ICECAST_HOST", "").strip()
    password = os.environ.get("ICECAST_PASSWORD", "").strip()
    mount = os.environ.get("ICECAST_MOUNT", "/stream").strip()
    user = os.environ.get("ICECAST_USER", "source").strip()
    port_str = os.environ.get("ICECAST_PORT", "80").strip()

    if not host:
        raise SystemExit(
            "Icecast not configured. Set either ZENO_SOURCE_URL or "
            "ICECAST_HOST + ICECAST_PORT + ICECAST_PASSWORD + ICECAST_MOUNT."
        )
    if not password:
        raise SystemExit("ICECAST_PASSWORD must be set.")

    try:
        port = int(port_str)
    except ValueError:
        raise SystemExit(f"ICECAST_PORT must be an integer, got: {port_str!r}")

    if not mount.startswith("/"):
        mount = "/" + mount

    return IcecastTarget(user=user, password=password,
                         host=host, port=port, mount=mount)


def resolve_track(query: str, requested_by: str) -> Optional[Track]:
    """Resolve a YouTube URL or search query to a Track via yt-dlp."""
    try:
        with YoutubeDL(YDL_OPTS) as ydl:
            info = ydl.extract_info(query, download=False)
        if info is None:
            return None
        if "entries" in info:
            entries = [e for e in info["entries"] if e]
            if not entries:
                return None
            info = entries[0]
        video_id = info.get("id")
        title = info.get("title") or "Unknown"
        duration = int(info.get("duration") or 0)
        page_url = (
            f"https://www.youtube.com/watch?v={video_id}"
            if video_id
            else info.get("webpage_url", query)
        )
        audio_url = info.get("url")
        if not audio_url:
            formats = info.get("formats") or []
            audio_only = [
                f for f in formats
                if f.get("acodec") and f.get("acodec") != "none"
                and (f.get("vcodec") in (None, "none"))
            ]
            audio_only.sort(key=lambda f: f.get("abr") or 0, reverse=True)
            if audio_only:
                audio_url = audio_only[0].get("url")
        if not audio_url:
            return None
        return Track(
            title=title,
            page_url=page_url,
            audio_url=audio_url,
            duration=duration,
            requested_by=requested_by,
        )
    except Exception as exc:
        print(f"[resolve] error: {exc!r}")
        return None


class DJBot(BaseBot):
    def __init__(self, target: IcecastTarget) -> None:
        super().__init__()
        self.target = target
        self.player = Player()
        self._lock = asyncio.Lock()

    async def on_start(self, session_metadata: SessionMetadata) -> None:  # type: ignore[override]
        await self.highrise.chat(
            "DJ bot online. Commands: !play <url|query>, !skip, !stop, !queue"
        )

    async def on_chat(self, user: User, message: str) -> None:
        msg = message.strip()
        if not msg.startswith("!"):
            return
        parts = msg.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd == "!play":
            await self.cmd_play(user, arg)
        elif cmd == "!skip":
            await self.cmd_skip(user)
        elif cmd == "!stop":
            await self.cmd_stop(user)
        elif cmd == "!queue":
            await self.cmd_queue(user)

    async def cmd_play(self, user: User, arg: str) -> None:
        if not arg:
            await self.highrise.chat("Usage: !play <youtube url or search query>")
            return
        await self.highrise.chat(f"Searching: {arg[:80]}")
        track = await asyncio.to_thread(resolve_track, arg, user.username)
        if track is None:
            await self.highrise.chat("Could not find that track.")
            return
        async with self._lock:
            self.player.queue.append(track)
            position = len(self.player.queue)
            should_start = self.player.current is None
        if should_start:
            await self._advance()
        else:
            await self.highrise.chat(
                f"Queued #{position}: {track.display()} (by {user.username})"
            )

    async def cmd_skip(self, user: User) -> None:
        if self.player.current is None:
            await self.highrise.chat("Nothing is playing.")
            return
        await self.highrise.chat(f"{user.username} skipped: {self.player.current.title}")
        await self._kill_ffmpeg()

    async def cmd_stop(self, user: User) -> None:
        async with self._lock:
            self.player.queue.clear()
            had_current = self.player.current is not None
            self.player.stop_requested = True
        await self._kill_ffmpeg()
        async with self._lock:
            self.player.current = None
            self.player.stop_requested = False
        if had_current:
            await self.highrise.chat(f"{user.username} stopped playback. Queue cleared.")
        else:
            await self.highrise.chat("Queue cleared.")

    async def cmd_queue(self, user: User) -> None:
        lines: list[str] = []
        if self.player.current:
            lines.append(f"Now: {self.player.current.display()}")
        else:
            lines.append("Now: (nothing)")
        if self.player.queue:
            for i, t in enumerate(self.player.queue[:10], start=1):
                lines.append(f"{i}. {t.display()} - {t.requested_by}")
            extra = len(self.player.queue) - 10
            if extra > 0:
                lines.append(f"(+{extra} more)")
        else:
            lines.append("Queue is empty.")
        for line in lines:
            await self.highrise.chat(line)

    async def _advance(self) -> None:
        async with self._lock:
            if self.player.stop_requested:
                self.player.current = None
                return
            if not self.player.queue:
                self.player.current = None
                await self.highrise.chat("Queue finished.")
                return
            track = self.player.queue.pop(0)
            self.player.current = track
        await self.highrise.chat(f"Now playing: {track.display()}")
        self.player.play_task = asyncio.create_task(self._play_track(track))

    async def _play_track(self, track: Track) -> None:
        ffmpeg_cmd = self._ffmpeg_cmd(track)
        curl_cmd = self._curl_cmd(track)
        try:
            ffmpeg_proc = await asyncio.create_subprocess_exec(
                *ffmpeg_cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            assert ffmpeg_proc.stdout is not None
            curl_proc = await asyncio.create_subprocess_exec(
                *curl_cmd,
                stdin=ffmpeg_proc.stdout,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as e:
            await self.highrise.chat(f"Required binary missing: {e.filename}")
            async with self._lock:
                self.player.current = None
            return

        self.player.ffmpeg = ffmpeg_proc
        self.player.curl = curl_proc

        async def drain(stream: asyncio.StreamReader) -> bytes:
            chunks: list[bytes] = []
            while True:
                line = await stream.readline()
                if not line:
                    break
                chunks.append(line)
            return b"".join(chunks[-10:])

        ff_err_task = asyncio.create_task(drain(ffmpeg_proc.stderr))  # type: ignore[arg-type]
        curl_err_task = asyncio.create_task(drain(curl_proc.stderr))  # type: ignore[arg-type]

        curl_rc = await curl_proc.wait()
        if ffmpeg_proc.returncode is None:
            try:
                ffmpeg_proc.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(ffmpeg_proc.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                try:
                    ffmpeg_proc.kill()
                except ProcessLookupError:
                    pass
                await ffmpeg_proc.wait()

        ff_rc = ffmpeg_proc.returncode
        ff_err = await ff_err_task
        curl_err = await curl_err_task

        async with self._lock:
            self.player.ffmpeg = None
            self.player.curl = None
            current_finished = self.player.current is track
            if current_finished:
                self.player.current = None

        if not self.player.stop_requested:
            failed = curl_rc not in (0,) or (
                ff_rc not in (0, -9, -15) and ff_rc is not None
            )
            if failed:
                tail = (
                    curl_err.decode(errors="replace").strip().splitlines()
                    or ff_err.decode(errors="replace").strip().splitlines()
                )
                last = tail[-1] if tail else f"curl rc={curl_rc}, ffmpeg rc={ff_rc}"
                print(f"[stream] track failed: {last}")
                await self.highrise.chat(f"Stream error on '{track.title}', skipping.")

        if current_finished and not self.player.stop_requested:
            await self._advance()

    async def _kill_ffmpeg(self) -> None:
        for proc in (self.player.curl, self.player.ffmpeg):
            if proc is None or proc.returncode is not None:
                continue
            try:
                proc.terminate()
            except ProcessLookupError:
                continue
            try:
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                await proc.wait()

    def _ffmpeg_cmd(self, track: Track) -> list[str]:
        return [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "warning",
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5",
            "-re",
            "-i", track.audio_url,
            "-vn",
            "-c:a", "libmp3lame",
            "-b:a", "128k",
            "-ar", "44100",
            "-ac", "2",
            "-f", "mp3",
            "-",
        ]

    def _curl_cmd(self, track: Track) -> list[str]:
        return [
            "curl",
            "--silent",
            "--show-error",
            "--fail",
            "--user", f"{self.target.user}:{self.target.password}",
            "--header", "Content-Type: audio/mpeg",
            "--header", "Ice-Name: DJ Bot",
            "--header", "Ice-Description: " + _safe_header(track.title)[:120],
            "--header", "Ice-Public: 1",
            "--header", "Expect:",
            "--request", "PUT",
            "--upload-file", "-",
            self.target.http_url,
        ]


def run() -> None:
    token = (
        os.environ.get("HIGHRISE_TOKEN")
        or os.environ.get("HIGHRISE_BOT_TOKEN")
        or ""
    ).strip()
    room_id = (
        os.environ.get("ROOM_ID")
        or os.environ.get("HIGHRISE_ROOM_ID")
        or ""
    ).strip()

    if not token:
        raise SystemExit(
            "Bot token not set. Set HIGHRISE_TOKEN (or HIGHRISE_BOT_TOKEN)."
        )
    if not room_id:
        raise SystemExit(
            "Room ID not set. Set ROOM_ID (or HIGHRISE_ROOM_ID)."
        )

    target = build_target_from_env()
    print(f"[bot] streaming to {target.http_url} as '{target.user}'")

    bot = DJBot(target=target)
    definitions = [BotDefinition(bot, room_id, token)]

    while True:
        try:
            asyncio.run(highrise_main(definitions))
        except KeyboardInterrupt:
            break
        except Exception as exc:
            print(f"[bot] disconnected: {exc!r} — reconnecting in 5s")
            time.sleep(5)


if __name__ == "__main__":
    run()
