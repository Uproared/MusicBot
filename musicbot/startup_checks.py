"""
Startup checks that always run, even with run.py --no-checks (S7).
They only log; nothing here stops the bot.
"""

import importlib.metadata
import logging
import os
import pathlib
import shutil
import subprocess
import sys
from typing import TYPE_CHECKING, Iterable, Tuple

import discord

if TYPE_CHECKING:
    from .config import Config

log = logging.getLogger(__name__)


def _package_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "not installed"


def _ffmpeg_output(*args: str) -> str:
    """Run ffmpeg with `args` and return its stdout, or "" if that fails."""
    exe = shutil.which("ffmpeg")
    if not exe:
        return ""
    try:
        return subprocess.run(
            [exe, "-hide_banner", *args],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        log.warning("Could not run ffmpeg for the startup checks.", exc_info=True)
        return ""


def log_versions() -> None:
    """Log the versions that matter for playback, to match problems to upgrades."""
    ffmpeg_lines = _ffmpeg_output("-version").splitlines()
    log.info(
        "Versions: Python %(py)s, discord.py %(dpy)s, davey %(davey)s, "
        "yt-dlp %(ytdlp)s, yt-dlp-ejs %(ejs)s, %(ffmpeg)s",
        {
            "py": sys.version.split()[0],
            "dpy": discord.__version__,
            "davey": _package_version("davey"),
            "ytdlp": _package_version("yt-dlp"),
            "ejs": _package_version("yt-dlp-ejs"),
            "ffmpeg": ffmpeg_lines[0] if ffmpeg_lines else "ffmpeg not found",
        },
    )


def check_ffmpeg_tools(use_opus_encoder: bool) -> None:
    """Warn about missing ffprobe, or an ffmpeg without the libopus encoder."""
    if not shutil.which("ffprobe"):
        log.warning(
            "ffprobe was not found on PATH. It is used to probe media and to "
            "find track durations when the site doesn't report one."
        )
    if use_opus_encoder:
        encoders = _ffmpeg_output("-encoders")
        if encoders and "libopus" not in encoders:
            log.error(
                "This ffmpeg has no libopus encoder, which UseOpusAudio needs; "
                "playback will fail. Install an ffmpeg build with libopus, "
                "or set UseOpusAudio = no."
            )


def check_private_files(files: Iterable[Tuple[str, pathlib.Path]]) -> None:
    """Warn if a file holding secrets is readable by other users (POSIX only)."""
    if os.name != "posix":
        return
    for label, path in files:
        try:
            if path.is_file() and path.stat().st_mode & 0o077:
                log.warning(
                    "The %(label)s file is readable by other users on this machine: "
                    "%(path)s  (fix: chmod 600 %(path)s)",
                    {"label": label, "path": path},
                )
        except OSError:
            log.debug("Could not check permissions of %s", path, exc_info=True)


def check_deno_path(deno_path: str) -> None:
    """Error if YtdlpDenoPath is set but there's no deno there (C4)."""
    if not deno_path:
        return
    path = pathlib.Path(os.path.expanduser(deno_path))
    # yt-dlp runs <folder>/deno for a folder; Windows adds .exe by itself.
    exe = path.joinpath("deno") if path.is_dir() else path
    candidates = [exe]
    if sys.platform == "win32":
        candidates.append(pathlib.Path(f"{exe}.exe"))
    if not any(c.is_file() for c in candidates):
        log.error(
            "YtdlpDenoPath is set to %s, but there is no deno program there. "
            "YouTube playback may fail. Fix the path, or leave it blank to use PATH.",
            path,
        )


def run_startup_checks(config: "Config") -> None:
    """Run all startup checks. Only logs."""
    log_versions()
    check_ffmpeg_tools(config.use_opus_audio or config.use_opus_probe)
    check_private_files(
        [
            ("cookies", config.cookies_path),
            ("options (has the bot token)", config.config_file),
        ]
    )
    check_deno_path(config.ytdlp_deno_path)
