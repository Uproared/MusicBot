"""
B10 and the S1 follow-ups in MusicPlayer._on_playback_finished and
MusicBot.on_player_error, with stand-ins for the bot, playlist and entries.
"""

import asyncio
from collections import deque
from types import SimpleNamespace

import pytest

from musicbot import exceptions
from musicbot.player import MusicPlayer


class FakeBot:
    def __init__(self, loop, save_videos=True):
        self.loop = loop
        self.config = SimpleNamespace(default_volume=0.15, save_videos=save_videos)
        self.tasks = []

    def create_task(self, coro, name=None):
        self.tasks.append(name)
        coro.close()


class FakePlaylist:
    def __init__(self, *entries):
        self.entries = deque(entries)

    def on(self, *_args):
        return self


def make_player(loop, *queued, save_videos=True):
    player = MusicPlayer(FakeBot(loop, save_videos), SimpleNamespace(), FakePlaylist(*queued))
    events = []
    player.on("error", lambda **kw: events.append(("error", kw["entry"])))
    player.on("finished-playing", lambda **kw: events.append(("finished", kw["entry"])))
    return player, events


def entry(name):
    return SimpleNamespace(name=name)


def test_repeat_requeues_after_success(current_loop):
    a = entry("a")
    player, events = make_player(current_loop)
    player.repeatsong = True
    player._current_entry = a
    player._on_playback_finished(None)
    assert list(player.playlist.entries) == [a]
    assert events == [("finished", a)]


def test_repeat_does_not_loop_a_failed_track(current_loop):
    a = entry("a")
    player, events = make_player(current_loop)
    player.repeatsong = True
    player._current_entry = a
    player._on_playback_finished(exceptions.FFmpegError("x"))
    assert not player.playlist.entries
    assert events == [("error", a)]


def test_loopqueue_drops_a_failed_track(current_loop):
    a, b = entry("a"), entry("b")
    player, _ = make_player(current_loop, b)
    player.loopqueue = True
    player._current_entry = a
    player._on_playback_finished(exceptions.FFmpegError("x"))
    assert list(player.playlist.entries) == [b]


@pytest.mark.parametrize("mode", ["repeatsong", "loopqueue"])
def test_seek_restart_is_not_queued_twice(current_loop, mode):
    """seek/speed/volume put the current entry back at the head, then skip()."""
    a, b = entry("a"), entry("b")
    player, events = make_player(current_loop, a, b)
    setattr(player, mode, True)
    player._current_entry = a
    player._on_playback_finished(None)
    assert list(player.playlist.entries) == [a, b]
    assert events == [("finished", a)]


@pytest.mark.parametrize("error", [None, exceptions.FFmpegError("x")])
def test_media_file_cleanup_runs_even_on_error(current_loop, error):
    a = entry("a")
    player, _ = make_player(current_loop, save_videos=False)
    player._current_entry = a
    player._on_playback_finished(error)
    assert player.bot.tasks == ["MB_CacheCleanup"]


def test_audio_thread_callback_runs_on_the_event_loop(current_loop):
    """B1: discord.py's `after` runs on its audio thread; the work must not run there."""
    import threading

    a = entry("a")
    player, events = make_player(current_loop)
    player._current_entry = a
    ran_on = []
    original = player._on_playback_finished
    player._on_playback_finished = lambda *args: (
        ran_on.append(threading.current_thread()),
        original(*args),
    )

    t = threading.Thread(target=player._playback_finished, args=(None,))
    t.start()
    t.join(5)
    assert not events, "the audio thread must only hand off the work"

    current_loop.run_until_complete(asyncio.sleep(0.05))
    assert events == [("finished", a)]
    assert ran_on == [threading.main_thread()]


def test_end_of_a_replaced_source_is_ignored(current_loop):
    a = entry("a")
    old_source, new_source = object(), object()
    player, events = make_player(current_loop)
    player._current_entry = a
    player._source = new_source
    player._on_playback_finished(None, old_source)
    assert not events
    assert player._current_entry is a and player._source is new_source


@pytest.mark.parametrize(
    "error,expect_file_update",
    [
        (exceptions.FFmpegError("ffmpeg exited with code 1."), False),
        (exceptions.ExtractionError("Video unavailable"), True),
    ],
)
def test_ffmpeg_error_does_not_edit_autoplaylist_file(error, expect_file_update):
    from musicbot.bot import MusicBot  # heavy import, only for this test

    calls = []

    class AutoPlaylist:
        async def remove_track(self, _subject, *, ex=None, delete_from_ap=False):
            calls.append((ex, delete_from_ap))

    fake_bot = SimpleNamespace(
        config=SimpleNamespace(remove_ap=True),
        server_data={1: SimpleNamespace(autoplaylist=AutoPlaylist())},
    )
    player = SimpleNamespace(
        voice_client=SimpleNamespace(guild=SimpleNamespace(id=1)), is_dead=True
    )
    ap_entry = SimpleNamespace(
        channel=None,
        from_auto_playlist=True,
        info=SimpleNamespace(input_subject="https://example.invalid/track"),
    )

    asyncio.run(
        MusicBot.on_player_error(fake_bot, player=player, entry=ap_entry, ex=error)
    )
    assert calls == [(error, expect_file_update)]


def test_no_now_playing_message_for_a_track_that_already_ended():
    """A failed track got "Playback failed", then a late "Now playing" for it."""
    from musicbot.bot import MusicBot  # heavy import, only for this test

    calls = []

    async def noop(*_args, **_kwargs):
        return None

    async def send(*args, **_kwargs):
        calls.append(("send", args))

    guild = SimpleNamespace(id=1)
    fake_bot = SimpleNamespace(
        server_data={1: SimpleNamespace()},
        config=SimpleNamespace(write_current_song=False),
        _handle_guild_auto_pause=noop,
        reset_player_inactivity=noop,
        update_now_playing_status=noop,
        serialize_queue=noop,
        safe_send_message=send,
    )
    ended = SimpleNamespace(channel=object(), author=object(), title="t", url="u")
    player = SimpleNamespace(
        voice_client=SimpleNamespace(channel=SimpleNamespace(guild=guild), guild=guild),
        skip_state=SimpleNamespace(reset=lambda: None),
        skip=lambda: calls.append(("skip",)),
        current_entry=None,  # the track already ended (or failed) during the awaits
    )

    asyncio.run(MusicBot.on_player_play(fake_bot, player=player, entry=ended))
    assert calls == []
