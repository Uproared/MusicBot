"""
Batch 4: voice resilience (B2, B4, B8, C2), with stand-ins for Discord objects.
"""

import asyncio
from types import SimpleNamespace

import discord
import pytest

from musicbot import exceptions
from musicbot.entry import URLPlaylistEntry
from test_player_finished import make_player  # pylint: disable=import-error


class FakeURLEntry(URLPlaylistEntry):
    """A URLPlaylistEntry without the yt-dlp info it normally needs."""

    title = "t"

    def __init__(self, duration):  # pylint: disable=super-init-not-called
        self._dur = duration
        self.started_at = None

    @property
    def duration(self):
        return self._dur

    def set_start_time(self, start_time):
        self.started_at = start_time


# --- B2: a voice drop longer than VoiceClient.timeout ------------------------


def test_voice_drop_requeues_the_track_at_its_position(current_loop):
    track = FakeURLEntry(duration=200)
    player, events = make_player(current_loop, save_videos=False, connected=False)
    player._current_entry = track
    player._source = SimpleNamespace(progress=42.0)

    player._on_playback_finished(None)

    assert list(player.playlist.entries) == [track]
    assert track.started_at == 42.0
    assert events == []  # not "finished-playing"
    assert player.is_stopped and player.current_entry is None
    # the media file is kept for the resume, and a resume is scheduled
    assert player.bot.tasks == ["MB_ResumeAfterVoiceDrop"]


def test_skip_while_voice_is_down_is_still_a_skip(current_loop):
    track = FakeURLEntry(duration=200)
    player, events = make_player(current_loop, connected=False)
    player._current_entry = track
    player._source = SimpleNamespace(progress=42.0)
    player._stop_requested = True  # set by skip()/stop() via _kill_current_player

    player._on_playback_finished(None)

    assert not player.playlist.entries
    assert events == [("finished", track)]


def test_track_that_had_finished_is_not_requeued(current_loop):
    track = FakeURLEntry(duration=200)
    player, events = make_player(current_loop, connected=False)
    player._current_entry = track
    player._source = SimpleNamespace(progress=199.0)

    player._on_playback_finished(None)

    assert not player.playlist.entries
    assert events == [("finished", track)]


def test_kill_current_player_marks_the_stop_as_requested(current_loop):
    player, _ = make_player(current_loop)
    player._current_player = SimpleNamespace(stop=lambda: None)
    player._kill_current_player()
    assert player._stop_requested


def run_resume_watch(loop, player, monkeypatch):
    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_wait)
    loop.run_until_complete(player._resume_after_voice_drop())


def test_resume_once_voice_reconnects(current_loop, monkeypatch):
    track = FakeURLEntry(duration=200)
    player, _ = make_player(current_loop, track)
    checks = iter([False, False, True])
    player.voice_client = SimpleNamespace(is_connected=lambda: next(checks))
    played = []
    player.play = lambda *a, **k: played.append(True)

    run_resume_watch(current_loop, player, monkeypatch)
    assert played == [True]


def test_resume_watch_stops_if_playback_was_restarted(current_loop, monkeypatch):
    track = FakeURLEntry(duration=200)
    player, _ = make_player(current_loop, track)
    player.state = player.state.__class__.PLAYING  # e.g. the user ran summon
    player.play = lambda *a, **k: pytest.fail("must not restart playback")

    run_resume_watch(current_loop, player, monkeypatch)


# --- B4: get_voice_client after the last retry ------------------------------


class FakeVoiceChannel(discord.VoiceChannel):
    """Passes MusicBot's isinstance check; connect() fails the given way."""

    def __init__(self, connect_error):  # pylint: disable=super-init-not-called
        self.name = "General"
        self.guild = SimpleNamespace(me=None, voice_client=None, name="g")
        self.connect_error = connect_error
        self.attempts = 0

    def __repr__(self):
        return "<FakeVoiceChannel>"

    def permissions_for(self, _member):
        return SimpleNamespace(connect=True, speak=True)

    async def connect(self, **_kwargs):
        self.attempts += 1
        raise self.connect_error


def call_get_voice_client(channel):
    from musicbot.bot import MusicBot  # heavy import, only for these tests

    fake_bot = SimpleNamespace(config=SimpleNamespace(self_deafen=True, debug_mode=False))
    return asyncio.run(MusicBot.get_voice_client(fake_bot, channel))


def test_connect_timeouts_end_in_a_command_error():
    from musicbot.constants import VOICE_CLIENT_MAX_RETRY_CONNECT

    channel = FakeVoiceChannel(asyncio.TimeoutError())
    with pytest.raises(exceptions.CommandError) as info:
        call_get_voice_client(channel)  # used to raise UnboundLocalError
    assert "could not connect" in info.value.message
    assert channel.attempts == VOICE_CLIENT_MAX_RETRY_CONNECT


def test_connect_client_exception_is_a_command_error():
    channel = FakeVoiceChannel(discord.ClientException("Already connected to a voice channel."))
    with pytest.raises(exceptions.CommandError):
        call_get_voice_client(channel)
    assert channel.attempts == 1


def test_connect_timeout_is_not_short():
    """discord.py reuses it for reconnects and for playback's reconnect wait (B2)."""
    from musicbot.constants import VOICE_CLIENT_RECONNECT_TIMEOUT

    assert VOICE_CLIENT_RECONNECT_TIMEOUT >= 30


# --- B8 and C2: reconnecting voice clients ----------------------------------


class FakeVoiceClient(discord.VoiceClient):
    def __init__(self, connected, connection=None):  # pylint: disable=super-init-not-called
        self._is_connected = connected
        self._connection = connection or SimpleNamespace(ws=discord.utils.MISSING, state=None)
        self.channel = SimpleNamespace(name="General")

    def is_connected(self):
        return self._is_connected


class Guild:
    """Hashable, as MusicBot uses guilds as dict keys."""

    def __init__(self, voice_client, voice_channel):
        self.id = 1
        self.name = "g"
        self.unavailable = False
        self.voice_client = voice_client
        self.me = SimpleNamespace(voice=SimpleNamespace(channel=voice_channel))


def test_auto_join_leaves_a_reconnecting_voice_client_alone():
    """B8: on_resumed used to discard the player (and its queue) mid-reconnect."""
    from musicbot.bot import MusicBot

    calls = []

    async def record_disconnect(guild):
        calls.append(("disconnect", guild))

    guild = Guild(FakeVoiceClient(connected=False), voice_channel=SimpleNamespace(name="General"))
    fake_bot = SimpleNamespace(
        guilds=[guild],
        server_data={1: SimpleNamespace(auto_join_channel=None, follow_user=None)},
        config=SimpleNamespace(auto_summon=False),
        get_player_in=lambda g: object(),
        disconnect_voice_client=record_disconnect,
    )
    asyncio.run(MusicBot._auto_join_channels(fake_bot, from_resume=True))
    assert calls == []


def test_api_disconnect_handles_a_voice_client_that_never_connected():
    """C2: reading the private ws._close_code used to raise on the MISSING placeholder."""
    from musicbot.bot import MusicBot

    calls = []

    async def record_disconnect(guild):
        calls.append(guild)

    guild = SimpleNamespace(id=1, name="g", voice_client=FakeVoiceClient(connected=False))
    fake_bot = SimpleNamespace(
        get_guild=lambda _id: guild,
        players={1: object()},
        disconnect_voice_client=record_disconnect,
        server_data={1: SimpleNamespace(auto_join_channel=None)},
    )
    before = SimpleNamespace(channel=SimpleNamespace(guild=guild, name="General", id=5))
    assert asyncio.run(MusicBot._handle_api_disconnect(fake_bot, before)) is True
    assert calls == [guild]
