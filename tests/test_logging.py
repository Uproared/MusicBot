"""
Batch 6: logging and silent-failure coverage (S3-S7).
"""

import asyncio
import logging
import os
from types import SimpleNamespace

import pytest

from musicbot import startup_checks
from musicbot.downloader import YtdlpLogHook
from musicbot.lib.event_emitter import EventEmitter


# --- S3: yt-dlp output levels -----------------------------------------------


def test_ytdlp_progress_is_debug_and_warnings_are_warnings(caplog):
    """The logger is now always attached; progress must not flood INFO."""
    caplog.set_level(logging.DEBUG, logger="musicbot.downloader")
    hook = YtdlpLogHook()
    hook.debug("[youtube] abc: Downloading webpage")
    hook.warning("No supported JavaScript runtime could be found")
    levels = {r.getMessage(): r.levelno for r in caplog.records}
    assert levels["[YTDLP] [youtube] abc: Downloading webpage"] == logging.DEBUG
    assert levels["[YTDLP] No supported JavaScript runtime could be found"] == logging.WARNING


# --- S5: event handler errors ------------------------------------------------


def test_async_event_handler_error_is_logged(current_loop, caplog):
    emitter = EventEmitter()

    async def on_play(**_kw):
        raise KeyError("author")  # like B5's skip-when-absent message

    emitter.on("play", on_play)
    emitter.emit("play")
    current_loop.run_until_complete(asyncio.sleep(0.01))

    # MusicBot's own record, not asyncio's "Task exception was never retrieved"
    # at garbage collection (which is all the old code produced, if anything).
    errors = [
        r
        for r in caplog.records
        if r.levelno == logging.ERROR and r.name == "musicbot.lib.event_emitter"
    ]
    assert errors and "on_play" in errors[0].getMessage()
    assert isinstance(errors[0].exc_info[1], KeyError)


def test_sync_event_handler_error_is_logged(current_loop, caplog):
    emitter = EventEmitter()

    def on_stop(**_kw):
        raise ValueError("boom")

    emitter.on("stop", on_stop)
    emitter.emit("stop")
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert errors and "stop event handler on_stop" in errors[0].getMessage()


def test_cancelled_event_handler_is_not_an_error(current_loop, caplog):
    emitter = EventEmitter()

    async def slow(**_kw):
        await asyncio.sleep(10)

    emitter.on("play", slow)
    emitter.emit("play")
    for task in list(emitter._task_pool):
        task.cancel()
    current_loop.run_until_complete(asyncio.sleep(0.01))
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


# --- S6: player watchdog -----------------------------------------------------


def fake_player(stopped=True, queued=1, starting=False, connected=True):
    plays = []
    player = SimpleNamespace(
        is_stopped=stopped,
        playlist=SimpleNamespace(entries=[object()] * queued),
        is_starting=starting,
        guild_or_net_unavailable=False,
        voice_client=SimpleNamespace(is_connected=lambda: connected),
        play=lambda: plays.append(True),
    )
    return player, plays


def watchdog_check(players, suspects):
    from musicbot.bot import MusicBot  # heavy import, only for these tests

    fake_bot = SimpleNamespace(players=players)
    return MusicBot._restart_stalled_players(fake_bot, suspects)


def test_watchdog_restarts_a_player_stalled_for_two_checks():
    player, plays = fake_player()
    suspects = watchdog_check({1: player}, set())
    assert suspects == {1} and plays == []  # first sighting: could be the gap between songs
    watchdog_check({1: player}, suspects)
    assert plays == [True]


@pytest.mark.parametrize(
    "state",
    [
        {"stopped": False},  # playing or paused
        {"queued": 0},  # nothing to play
        {"starting": True},  # downloading the next song
        {"connected": False},  # voice is down; the voice-drop resume handles it
    ],
)
def test_watchdog_leaves_healthy_players_alone(state):
    player, plays = fake_player(**state)
    suspects = watchdog_check({1: player}, {1})
    assert suspects == set() and plays == []


# --- S4: no file paths in playback error messages ----------------------------


@pytest.fixture
def i18n():
    from musicbot.i18n import I18n

    I18n()


def test_playback_error_message_hides_unexpected_error_details(i18n):
    from musicbot.bot import MusicBot

    sent = []

    async def send(_channel, response, **_kwargs):
        sent.append(response.content if hasattr(response, "content") else str(response))

    fake_bot = SimpleNamespace(
        server_data={1: SimpleNamespace(lang_code="", guild_id=1)},
        safe_send_message=send,
        config=SimpleNamespace(delete_delay_long=0, remove_ap=False),
    )
    player = SimpleNamespace(voice_client=SimpleNamespace(guild=SimpleNamespace(id=1)), is_dead=True)
    entry = SimpleNamespace(channel=object(), title="song", url="u", from_auto_playlist=False)
    ex = FileNotFoundError(2, "No such file", os.path.join("home", "ubuntu", "secret", "x.webm"))

    asyncio.run(MusicBot.on_player_error(fake_bot, player=player, entry=entry, ex=ex))
    assert len(sent) == 1
    assert "secret" not in sent[0] and "FileNotFoundError" in sent[0]


# --- S7: startup checks ------------------------------------------------------


def test_versions_are_logged(caplog):
    caplog.set_level(logging.INFO, logger="musicbot.startup_checks")
    startup_checks.log_versions()
    line = caplog.records[-1].getMessage()
    assert "discord.py 2.7.1" in line and "davey " in line and "yt-dlp " in line


def test_missing_libopus_encoder_is_an_error(monkeypatch, caplog):
    monkeypatch.setattr(startup_checks.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(startup_checks, "_ffmpeg_output", lambda *a: " A....D aac  AAC\n")
    startup_checks.check_ffmpeg_tools(use_opus_encoder=True)
    assert any("no libopus encoder" in r.getMessage() for r in caplog.records)


def test_missing_ffprobe_is_a_warning(monkeypatch, caplog):
    monkeypatch.setattr(startup_checks.shutil, "which", lambda name: None)
    startup_checks.check_ffmpeg_tools(use_opus_encoder=False)
    assert any("ffprobe was not found" in r.getMessage() for r in caplog.records)


@pytest.mark.skipif(os.name != "posix", reason="file modes are POSIX-only")
def test_group_readable_cookie_file_is_flagged(tmp_path, caplog):
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("x")
    cookies.chmod(0o644)
    startup_checks.check_private_files([("cookies", cookies)])
    assert any("chmod 600" in r.getMessage() for r in caplog.records)
