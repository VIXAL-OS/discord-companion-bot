"""
YouTube Transcription Module
============================

Detects YouTube URLs in Discord messages, downloads audio via yt-dlp,
transcribes via local Whisper CLI, and persists the transcript as a
markdown file in `data/transcripts/<video_id>.md`.

Architecture:
    YoutubeTranscriber           — top-level coordinator, holds per-channel locks
        .extract_video_id(text)  — regex over message content
        .existing_transcript_path(video_id) — returns Path or None for cache hit
        .transcribe(video_id, channel_id, on_progress=...) — async; returns Path

    Per-channel serialization:
        One transcription per channel at a time (whisper is CPU/GPU-bound).
        Additional requests in the same channel wait on a per-channel asyncio.Lock.
        Queue depth is announced via on_progress when the request is queued.

Storage layout:
    data/transcripts/<video_id>.md   — final markdown (front-matter + body)
    data/transcripts/.tmp/            — scratch dir for downloaded audio; cleaned on success

Config flags (read from config.json — see Bot.__init__):
    youtube_allow_age_restricted: bool — tries `--cookies-from-browser chrome` when True
    youtube_max_duration_s: int        — refuse videos longer than this (default 7200 = 2hrs)
    youtube_whisper_model: str|None    — whisper model name (default: 'small'; matches existing cache)
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
from pathlib import Path
from typing import Awaitable, Callable, Dict, Optional

logger = logging.getLogger(__name__)


# Matches youtube.com/watch?v=VID, youtu.be/VID, youtube.com/shorts/VID, youtube.com/embed/VID
# Captures the 11-char video ID. URL params (&t=, &si=, &list=) are ignored.
YOUTUBE_URL_RE = re.compile(
    r"(?:https?://)?"
    r"(?:www\.|m\.)?"
    r"(?:youtube\.com/(?:watch\?v=|shorts/|embed/|live/)|youtu\.be/)"
    r"([A-Za-z0-9_-]{11})"
)


class TranscribeError(Exception):
    """Raised for any user-facing transcription failure."""


class VideoTooLong(TranscribeError):
    pass


class VideoUnavailable(TranscribeError):
    pass


class YoutubeTranscriber:
    def __init__(
        self,
        transcripts_dir: Path,
        *,
        allow_age_restricted: bool = False,
        max_duration_s: int = 7200,
        whisper_model: str = "small",
        whisper_executable: str = "whisper",
        yt_dlp_executable: str = "yt-dlp",
    ):
        self.transcripts_dir = Path(transcripts_dir)
        self.transcripts_dir.mkdir(parents=True, exist_ok=True)
        self.tmp_dir = self.transcripts_dir / ".tmp"
        self.tmp_dir.mkdir(exist_ok=True)
        self.allow_age_restricted = allow_age_restricted
        self.max_duration_s = max_duration_s
        self.whisper_model = whisper_model
        self.whisper_executable = whisper_executable
        self.yt_dlp_executable = yt_dlp_executable
        # Per-channel serialization. New request waits on this lock if another
        # transcription is in flight for the same channel.
        self._channel_locks: Dict[int, asyncio.Lock] = {}
        # Per-channel queue depth (count of waiters), so we can announce
        # "queued behind N others" when a request enters a busy channel.
        self._channel_queue: Dict[int, int] = {}

    # --------------------------------------------------------------------- #
    # URL detection                                                          #
    # --------------------------------------------------------------------- #
    @staticmethod
    def extract_video_id(text: str) -> Optional[str]:
        """Return the first YouTube video ID found in `text`, or None."""
        m = YOUTUBE_URL_RE.search(text or "")
        return m.group(1) if m else None

    def existing_transcript_path(self, video_id: str) -> Optional[Path]:
        """Return the cached transcript Path if it exists, else None."""
        p = self.transcripts_dir / f"{video_id}.md"
        return p if p.exists() else None

    # --------------------------------------------------------------------- #
    # Main entry point                                                       #
    # --------------------------------------------------------------------- #
    async def transcribe(
        self,
        video_id: str,
        channel_id: int,
        on_progress: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> Path:
        """
        Transcribe a YouTube video by ID. Returns the path to the saved
        markdown transcript. Serializes per-channel so concurrent requests
        in the same channel queue rather than thrashing.

        `on_progress` is an optional async callback receiving short status
        strings ("Fetching video info…", "Transcribing…"). Used to edit a
        Discord "thinking" message in place.
        """
        lock = self._channel_locks.setdefault(channel_id, asyncio.Lock())
        self._channel_queue[channel_id] = self._channel_queue.get(channel_id, 0) + 1
        was_queued = lock.locked()
        try:
            if was_queued and on_progress:
                # Subtract 1 because we increment INCLUDING ourselves above —
                # "queued behind N" means N OTHER jobs ahead of us.
                ahead = self._channel_queue[channel_id] - 1
                await on_progress(f"Queued behind {ahead} other transcription(s) in this channel.")
            async with lock:
                return await self._transcribe_one(video_id, on_progress)
        finally:
            self._channel_queue[channel_id] = max(0, self._channel_queue.get(channel_id, 1) - 1)

    async def _transcribe_one(
        self,
        video_id: str,
        on_progress: Optional[Callable[[str], Awaitable[None]]],
    ) -> Path:
        url = f"https://www.youtube.com/watch?v={video_id}"

        # Step 1: fetch metadata. Cheap, gives us duration + title for the cap check.
        if on_progress:
            await on_progress("Fetching video info…")
        info = await asyncio.to_thread(self._yt_dlp_info, url)
        duration = int(info.get("duration") or 0)
        if duration > self.max_duration_s:
            raise VideoTooLong(
                f"Video is {duration // 60}m{duration % 60}s — over the {self.max_duration_s // 60}m cap."
            )

        title = info.get("title") or "(unknown title)"
        uploader = info.get("uploader") or "(unknown channel)"

        # Step 2: download audio. yt-dlp + ffmpeg do this in one shot.
        if on_progress:
            mm, ss = divmod(duration, 60)
            await on_progress(f"Downloading audio for **{title}** ({mm}m{ss:02d}s)…")
        audio_path = self.tmp_dir / f"{video_id}.mp3"
        try:
            await asyncio.to_thread(self._yt_dlp_download, url, audio_path)

            # Step 3: run whisper. Slow — give a realistic estimate.
            # Rough rule of thumb for `small` on CPU: ~0.5-1x realtime. With GPU
            # it's ~3-5x. We can't easily detect which, so we quote a wide range.
            if on_progress:
                est_lo = max(1, duration // 60 // 5)
                est_hi = max(2, duration // 60)
                await on_progress(f"Transcribing… (est ~{est_lo}-{est_hi} min on this hardware)")
            transcript_text = await asyncio.to_thread(self._run_whisper, audio_path)

            # Step 4: save markdown.
            transcript_path = self.transcripts_dir / f"{video_id}.md"
            await asyncio.to_thread(
                self._save_transcript,
                transcript_path,
                video_id=video_id,
                url=url,
                title=title,
                uploader=uploader,
                duration_s=duration,
                transcript=transcript_text,
            )
        finally:
            # Always remove the audio file — it's already huge (50-100 MB for a
            # 1-hour video) and we don't need it once whisper consumed it.
            try:
                if audio_path.exists():
                    audio_path.unlink()
            except OSError as e:
                logger.warning("Failed to clean up %s: %s", audio_path, e)

        return transcript_path

    # --------------------------------------------------------------------- #
    # Step implementations (sync — wrapped via asyncio.to_thread by caller)  #
    # --------------------------------------------------------------------- #
    def _yt_dlp_info(self, url: str) -> dict:
        """Get video metadata WITHOUT downloading. Returns yt-dlp's --dump-json output."""
        cmd = [self.yt_dlp_executable, "--dump-json", "--no-warnings", "--skip-download"]
        if self.allow_age_restricted:
            # Best-effort cookies for age-restricted videos. If Chrome isn't
            # running / user isn't logged in, yt-dlp silently skips cookies
            # and the request proceeds normally; only actually-age-restricted
            # videos will then fail at download time with a clearer error.
            cmd += ["--cookies-from-browser", "chrome"]
        cmd.append(url)
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
        except subprocess.TimeoutExpired:
            raise VideoUnavailable("Timed out fetching video metadata (network slow?).")
        if result.returncode != 0:
            stderr_tail = (result.stderr or "").strip().splitlines()[-3:]
            raise VideoUnavailable(
                "Couldn't read video info: " + (" | ".join(stderr_tail) or "yt-dlp failed silently")
            )
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as e:
            raise VideoUnavailable(f"yt-dlp returned malformed JSON: {e}")

    def _yt_dlp_download(self, url: str, audio_path: Path) -> None:
        """Download bestaudio and convert to mp3 at the specified path."""
        # yt-dlp adds the extension automatically based on `--audio-format`, so
        # we strip our hint and let it append `.mp3`. The output template
        # without extension is what `-o` expects when `--audio-format` is set.
        out_template = str(audio_path.with_suffix(""))
        cmd = [
            self.yt_dlp_executable,
            "-x",                              # extract audio
            "--audio-format", "mp3",
            "--audio-quality", "5",            # 0=best, 9=worst. 5 ≈ 128kbps — fine for speech.
            "--no-warnings",
            "--no-playlist",
            "-o", out_template + ".%(ext)s",
        ]
        if self.allow_age_restricted:
            cmd += ["--cookies-from-browser", "chrome"]
        cmd.append(url)
        try:
            # 30-minute hard cap on the download itself (network failure recovery).
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800, check=False)
        except subprocess.TimeoutExpired:
            raise VideoUnavailable("Audio download timed out after 30 minutes.")
        if result.returncode != 0:
            stderr_tail = (result.stderr or "").strip().splitlines()[-3:]
            raise VideoUnavailable(
                "Audio download failed: " + (" | ".join(stderr_tail) or "yt-dlp failed silently")
            )
        if not audio_path.exists():
            raise VideoUnavailable(f"Audio file missing after download: {audio_path}")

    def _run_whisper(self, audio_path: Path) -> str:
        """Invoke whisper CLI and return the resulting transcript text."""
        # whisper writes <input_stem>.txt into --output_dir
        cmd = [
            self.whisper_executable,
            str(audio_path),
            "--model", self.whisper_model,
            "--output_format", "txt",
            "--output_dir", str(self.tmp_dir),
            "--verbose", "False",
        ]
        try:
            # 4-hour hard cap (10x of the 2-hr video cap, give plenty of headroom)
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=14400, check=False)
        except subprocess.TimeoutExpired:
            raise TranscribeError("Whisper transcription exceeded the 4-hour cap.")
        if result.returncode != 0:
            stderr_tail = (result.stderr or "").strip().splitlines()[-5:]
            raise TranscribeError(
                "Whisper failed: " + (" | ".join(stderr_tail) or "no stderr")
            )
        txt_path = self.tmp_dir / f"{audio_path.stem}.txt"
        if not txt_path.exists():
            raise TranscribeError(f"Whisper output not found: {txt_path}")
        try:
            text = txt_path.read_text(encoding="utf-8")
        finally:
            try:
                txt_path.unlink()
            except OSError:
                pass
        return text.strip()

    def _save_transcript(
        self,
        path: Path,
        *,
        video_id: str,
        url: str,
        title: str,
        uploader: str,
        duration_s: int,
        transcript: str,
    ) -> None:
        """Write the final markdown with metadata front-matter."""
        from datetime import datetime, timezone
        mm, ss = divmod(int(duration_s), 60)
        hh, mm = divmod(mm, 60)
        if hh:
            duration_str = f"{hh}h{mm:02d}m{ss:02d}s"
        else:
            duration_str = f"{mm}m{ss:02d}s"
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        header = (
            f"# {title}\n\n"
            f"- **URL**: {url}\n"
            f"- **Channel**: {uploader}\n"
            f"- **Duration**: {duration_str}\n"
            f"- **Video ID**: `{video_id}`\n"
            f"- **Transcribed**: {ts}\n\n"
            f"---\n\n"
        )
        path.write_text(header + transcript + "\n", encoding="utf-8")
