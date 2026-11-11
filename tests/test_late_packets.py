"""
Late audio packet logging in SourcePlaybackCounter (stutter diagnosis).
"""

import gc
import logging
from types import SimpleNamespace

import pytest

import musicbot.player as player_mod
from musicbot.player import MusicPlayer, MusicPlayerState, SourcePlaybackCounter


@pytest.fixture(autouse=True)
def no_real_gc(monkeypatch):
    """Real collections during a test would add to the reported GC time."""
    monkeypatch.setattr(player_mod, "_GC_TIME", [0.0, 0.0])
    was_enabled = gc.isenabled()
    gc.disable()
    yield
    if was_enabled:
        gc.enable()


class FakeSource:
    def __init__(self, packets, on_read=None):
        self.left = packets
        self.on_read = on_read or (lambda n: None)
        self.n = 0

    def read(self):
        if self.left:
            self.left -= 1
            self.n += 1
            self.on_read(self.n)
            return b"x"
        return b""

    def cleanup(self):
        pass


def use_clock(monkeypatch, values):
    clock = iter(values)
    monkeypatch.setattr(player_mod, "time", SimpleNamespace(perf_counter=lambda: next(clock)))


def play(monkeypatch, done_times, read_secs=None, on_read=None, label="Song"):
    """
    One packet per entry in done_times (when its read finished, in seconds).
    read() reads the clock before and after the ffmpeg read; read_secs gives
    each read's duration (0 by default).
    """
    read_secs = read_secs or [0.0] * len(done_times)
    values = []
    for done, took in zip(done_times, read_secs):
        values += [done - took, done]
    values.append(done_times[-1])  # the final (empty) read
    use_clock(monkeypatch, values)
    counter = SourcePlaybackCounter(FakeSource(len(done_times), on_read), label=label)
    while counter.read():
        pass
    return counter


def lines(caplog, start):
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith(start)]


def test_late_packet_is_logged_with_position_and_title(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="musicbot.player")
    # 20 ms steady, one 80 ms gap at 0.12 s, then a 3.4 s gap (a pause or a
    # voice reconnect, not a stutter).
    play(monkeypatch, [0.0, 0.02, 0.04, 0.12, 0.14, 3.54, 3.56])
    assert lines(caplog, "Late audio packet:") == [
        "Late audio packet: 80 ms after the previous one (normal: 20 ms) at 0:00 in "
        "`Song`. Within that: ffmpeg read 0 ms, garbage collection 0 ms."
    ]
    assert lines(caplog, "Late audio packets in") == [
        "Late audio packets in `Song`: 1, worst 80 ms."
    ]


def test_late_packet_says_how_long_ffmpeg_and_gc_took(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="musicbot.player")

    def collect_during_packet_4(n):
        if n == 4:
            player_mod._GC_TIME[0] += 0.030  # a 30 ms collection

    # Packet 4 arrives 100 ms after packet 3; its own read took 40 ms.
    play(
        monkeypatch,
        [0.0, 0.02, 0.04, 0.14],
        read_secs=[0.0, 0.0, 0.0, 0.040],
        on_read=collect_during_packet_4,
    )
    assert lines(caplog, "Late audio packet:") == [
        "Late audio packet: 100 ms after the previous one (normal: 20 ms) at 0:00 in "
        "`Song`. Within that: ffmpeg read 40 ms, garbage collection 30 ms."
    ]


def test_steady_playback_logs_nothing(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="musicbot.player")
    play(monkeypatch, [i * 0.02 for i in range(200)])
    assert not lines(caplog, "Late audio packet")


def test_late_lines_are_rate_limited_but_all_are_counted(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="musicbot.player")
    play(monkeypatch, [0.1 * (i + 1) for i in range(10)])  # ten 100 ms gaps in 1 s
    assert len(lines(caplog, "Late audio packet:")) == 1
    assert lines(caplog, "Late audio packets in") == [
        "Late audio packets in `Song`: 9, worst 100 ms."
    ]


def test_reset_timing_hides_a_short_pause(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="musicbot.player")
    use_clock(monkeypatch, [0.0, 0.0, 0.5, 0.5])
    counter = SourcePlaybackCounter(FakeSource(2), label="Song")
    counter.read()
    counter.reset_timing()  # what MusicPlayer.resume() does
    counter.read()  # 500 ms later, but after a pause
    assert not lines(caplog, "Late audio packet")


def test_gc_callback_adds_up_collection_time():
    before = player_mod._GC_TIME[0]
    player_mod._time_gc("start", {})
    player_mod._time_gc("stop", {})
    assert player_mod._GC_TIME[0] >= before
    assert player_mod._time_gc in gc.callbacks


def test_resume_resets_timing_before_the_audio_thread_restarts(current_loop):
    counter = SourcePlaybackCounter(FakeSource(0), label="Song")
    counter._last_read = 1.0  # the last read before the pause
    seen = []
    voice_player = SimpleNamespace(resume=lambda: seen.append(counter._last_read))
    bot = SimpleNamespace(loop=current_loop, config=SimpleNamespace(default_volume=0.15))
    playlist = SimpleNamespace(on=lambda *_a: playlist)
    player = MusicPlayer(bot, SimpleNamespace(), playlist)
    player.state = MusicPlayerState.PAUSED
    player._current_player = voice_player
    player._source = counter
    player.resume()
    assert seen == [None]
