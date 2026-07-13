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
import os
import re
import secrets
import subprocess
import time
from collections import deque
from pathlib import Path
from typing import Awaitable, Callable, Dict, Optional

logger = logging.getLogger(__name__)


# Whisper's `--verbose True` prints one line per decoded segment, e.g.
#   [00:00.000 --> 00:05.000]  some text
#   [01:02:03.000 --> 01:02:08.500]  more text   (hours appear only past 60min)
# We parse the END timestamp (after `-->`) to learn how far whisper has chewed
# through the audio, which — against the known total duration — gives a real
# percentage for the progress bar.
_WHISPER_SEG_RE = re.compile(r"-->\s*(\d{1,2}:\d{2}(?::\d{2})?\.\d{3})\s*\]")


def _parse_whisper_ts(ts: str) -> float:
    """'MM:SS.mmm' or 'HH:MM:SS.mmm' -> seconds (float). 0.0 on malformed input."""
    try:
        nums = [float(p) for p in ts.split(":")]
    except ValueError:
        return 0.0
    if len(nums) == 3:
        h, m, s = nums
    elif len(nums) == 2:
        h, m, s = 0.0, nums[0], nums[1]
    else:
        return 0.0
    return h * 3600 + m * 60 + s


def _fmt_clock(seconds: float) -> str:
    """Seconds -> 'M:SS' (under an hour) or 'H:MM:SS'."""
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _progress_bar(pct: int, width: int = 16) -> str:
    """Unicode block bar, e.g. '████████░░░░░░░░' for 50%."""
    pct = max(0, min(100, pct))
    filled = int(round(pct / 100 * width))
    return "█" * filled + "░" * (width - filled)


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
        # Unique per-run scratch name. Keying the scratch files on the bare
        # video_id let two concurrent transcriptions of the SAME video share
        # one `.tmp/<id>.txt`; because whisper's output is read-then-deleted,
        # whichever run finished second found the file already gone and failed
        # with "Whisper output not found". A per-run suffix removes the shared
        # name entirely. The final transcript cache below stays keyed on the
        # bare video_id (that's the per-video cache, and it's write-once).
        scratch = f"{video_id}-{os.getpid()}-{secrets.token_hex(4)}"
        audio_path = self.tmp_dir / f"{scratch}.mp3"
        try:
            await asyncio.to_thread(self._yt_dlp_download, url, audio_path)

            # Step 3: run whisper. Slow — give a realistic estimate, then a live
            # progress bar as segments stream in.
            # Rough rule of thumb for `small` on CPU: ~0.5-1x realtime. With GPU
            # it's ~3-5x. We can't easily detect which, so we quote a wide range.
            if on_progress:
                est_lo = max(1, duration // 60 // 5)
                est_hi = max(2, duration // 60)
                await on_progress(
                    f"Transcribing **{title}**… {_progress_bar(0)} 0% "
                    f"(est ~{est_lo}-{est_hi} min on this hardware)"
                )

            # Bridge whisper's sync per-segment callback (runs in the worker
            # thread) back to the async on_progress callback on this loop.
            # Throttle so we don't spam Discord edits: emit only when the whole
            # percent advances AND at least 8s have passed since the last edit.
            loop = asyncio.get_running_loop()
            emit_state = {"pct": -1, "t": 0.0}

            def _on_segment(done_s: float, total_s: int) -> None:
                if not on_progress:
                    return
                pct = max(0, min(99, int(done_s / total_s * 100)))
                now = time.monotonic()
                if pct <= emit_state["pct"] or (now - emit_state["t"]) < 8:
                    return
                emit_state["pct"] = pct
                emit_state["t"] = now
                msg = (
                    f"Transcribing **{title}**… {_progress_bar(pct)} {pct}% "
                    f"({_fmt_clock(done_s)} / {_fmt_clock(total_s)})"
                )
                # Fire-and-forget; on_progress already swallows Discord errors.
                asyncio.run_coroutine_threadsafe(on_progress(msg), loop)

            transcript_text = await asyncio.to_thread(
                self._run_whisper, audio_path, duration, _on_segment
            )

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

    def _run_whisper(
        self,
        audio_path: Path,
        total_duration_s: int = 0,
        on_segment: Optional[Callable[[float, int], None]] = None,
    ) -> str:
        """Invoke whisper CLI and return the resulting transcript text.

        Streams whisper's per-segment output (`--verbose True`) so we can report
        live progress: for each decoded segment we parse the END timestamp and,
        if `on_segment` is supplied, call it with (seconds_done, total_seconds).
        `on_segment` is a SYNC callback (this method runs in a worker thread via
        asyncio.to_thread); the caller bridges it back to the event loop.

        The transcript itself is still read from whisper's `.txt` output file —
        we only use the streamed lines for progress, not for the final text.
        """
        # whisper writes <input_stem>.txt into --output_dir
        cmd = [
            self.whisper_executable,
            str(audio_path),
            "--model", self.whisper_model,
            "--output_format", "txt",
            "--output_dir", str(self.tmp_dir),
            "--verbose", "True",   # stream segments so we can build a progress bar
        ]
        # whisper writes <input_stem>.txt into --output_dir. audio_path is
        # unique per run (see _transcribe_one), so this path can't collide with
        # a concurrent transcription of the same video.
        txt_path = self.tmp_dir / f"{audio_path.stem}.txt"

        # One retry. A CLEAN whisper exit (returncode 0) that leaves no
        # transcript behind is almost always transient — resource starvation or
        # an interrupted write while the box is busy (MTG engine + XMage JVM).
        # Re-running on the already-downloaded audio is cheap and usually
        # succeeds. NON-zero exits and the 4-hour cap are deterministic and are
        # NOT retried. If the retry is also empty we raise whisper's own last
        # lines so the failure is diagnosable instead of a bare "not found"
        # (which is exactly what left us guessing before).
        last_tail = ""
        for attempt in range(2):
            deadline = time.monotonic() + 14400  # fresh 4h budget per attempt
            # Keep the last few lines so we can report something useful on
            # failure (stdout+stderr are merged, so the tail doubles as the
            # error context).
            tail: deque = deque(maxlen=8)
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    encoding="utf-8",
                    errors="replace",
                    # whisper is a Python program; force unbuffered stdout so its
                    # per-segment lines reach us immediately instead of block-
                    # buffering into chunks (smooth progress bar vs lurching one).
                    env={**os.environ, "PYTHONUNBUFFERED": "1"},
                )
            except FileNotFoundError:
                raise TranscribeError(f"whisper executable not found: {self.whisper_executable!r}")

            try:
                assert proc.stdout is not None
                for line in proc.stdout:
                    tail.append(line.rstrip())
                    if time.monotonic() > deadline:
                        proc.kill()
                        raise TranscribeError("Whisper transcription exceeded the 4-hour cap.")
                    if on_segment and total_duration_s > 0:
                        m = _WHISPER_SEG_RE.search(line)
                        if m:
                            done_s = _parse_whisper_ts(m.group(1))
                            try:
                                on_segment(done_s, total_duration_s)
                            except Exception as cb_err:
                                # A progress-callback failure must never abort the
                                # transcription itself.
                                logger.debug("whisper progress callback error: %s", cb_err)
                proc.wait()
            finally:
                if proc.poll() is None:
                    proc.kill()

            if proc.returncode != 0:
                tail_txt = " | ".join(t for t in list(tail)[-5:] if t)
                raise TranscribeError("Whisper failed: " + (tail_txt or "no output"))

            if txt_path.exists():
                try:
                    text = txt_path.read_text(encoding="utf-8")
                finally:
                    try:
                        txt_path.unlink()
                    except OSError:
                        pass
                return text.strip()

            # Clean exit, no transcript file. Stash whisper's last lines and
            # retry once; if the retry is also empty, they go into the error.
            last_tail = " | ".join(t for t in list(tail)[-5:] if t)
            logger.warning(
                "whisper exited 0 but wrote no transcript (attempt %d/2) for %s; "
                "last output: %s", attempt + 1, audio_path.name, last_tail or "(none)")

        raise TranscribeError(
            f"Whisper exited cleanly but produced no transcript at {txt_path} "
            f"after 2 attempts. Whisper's last output: "
            f"{last_tail or '(whisper printed nothing — likely killed or starved)'}"
        )

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
