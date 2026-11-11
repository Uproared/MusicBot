"""
Shared setup for MusicBot's offline tests.

Run from the repo root with a venv that has requirements.txt plus pytest:
    python -m pytest tests

Tests that need ffmpeg use bin/ffmpeg(.exe) if present, else ffmpeg on PATH,
and are skipped when neither exists. Nothing here connects to Discord.
"""

import asyncio
import os
import pathlib
import random
import shutil
import subprocess
import sys
import threading

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# MusicBot's modules call custom log levels (log.ffmpeg, log.noise, ...), which
# only exist once its logger class is installed, before those modules load.
from musicbot.logs import install_logger  # noqa: E402

install_logger()


def _find_ffmpeg_dir() -> str:
    exe = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    if (REPO_ROOT / "bin" / exe).is_file():
        return str(REPO_ROOT / "bin")
    found = shutil.which("ffmpeg")
    return os.path.dirname(found) if found else ""


@pytest.fixture(scope="session")
def ffmpeg_dir() -> str:
    """Directory holding ffmpeg, also put first on PATH for discord.py."""
    path = _find_ffmpeg_dir()
    if not path:
        pytest.skip("ffmpeg not found")
    os.environ["PATH"] = path + os.pathsep + os.environ.get("PATH", "")
    return path


@pytest.fixture(scope="session")
def media(ffmpeg_dir: str, tmp_path_factory: pytest.TempPathFactory) -> dict:
    """Small test inputs: a valid opus file, a truncated copy, garbage, a missing path."""
    out = tmp_path_factory.mktemp("media")
    good = out / "good.webm"
    subprocess.run(
        [
            os.path.join(ffmpeg_dir, "ffmpeg"),
            "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
            "-c:a", "libopus", "-b:a", "96k", str(good),
        ],
        check=True,
    )
    data = good.read_bytes()
    (out / "truncated.webm").write_bytes(data[: len(data) // 2])
    (out / "garbage.webm").write_bytes(random.Random(0).randbytes(20000))
    return {
        "good": str(good),
        "truncated": str(out / "truncated.webm"),
        "garbage": str(out / "garbage.webm"),
        "missing": str(out / "does_not_exist.webm"),
    }


@pytest.fixture
def event_loop_thread():
    """An asyncio loop running in a background thread, like discord.py's client loop."""
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    yield loop
    # Let calls already handed to the loop (discord.py's speak updates) finish.
    asyncio.run_coroutine_threadsafe(asyncio.sleep(0.05), loop).result(5)
    loop.call_soon_threadsafe(loop.stop)
    t.join(5)
    loop.close()


@pytest.fixture
def current_loop():
    """Set a current event loop: MusicPlayer's EventEmitter asks for one on creation."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    yield loop
    asyncio.set_event_loop(None)
    loop.close()
