"""
Late audio packet logging in SourcePlaybackCounter (stutter diagnosis).
"""

import logging
from types import SimpleNamespace

import musicbot.player as player_mod
from musicbot.player import MusicPlayer, MusicPlayerState, SourcePlaybackCounter


class FakeSource:
    def __init__(self, packets):
        self.left = packets

    def read(self):
        if self.left:
            self.left -= 1
            return b"x"
        return b""

    def cleanup(self):
        pass


def play(monkeypatch, read_times, label="Song"):
    """Read one packet per time in read_times (seconds), then the end."""
    clock = iter(read_times)
    monkeypatch.setattr(player_mod, "time", SimpleNamespace(perf_counter=lambda: next(clock)))
    counter = SourcePlaybackCounter(FakeSource(len(read_times)), label=label)
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
    late = lines(caplog, "Late audio packet:")
    assert late == [
        "Late audio packet: 80 ms after the previous one (normal: 20 ms) at 0:00 in `Song`."
    ]
    assert lines(caplog, "Late audio packets in") == [
        "Late audio packets in `Song`: 1, worst 80 ms."
    ]


def test_steady_playback_logs_nothing(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="musicbot.player")
    play(monkeypatch, [i * 0.02 for i in range(200)])
    assert not lines(caplog, "Late audio packet")


def test_late_lines_are_rate_limited_but_all_are_counted(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="musicbot.player")
    times, t = [], 0.0
    for _ in range(10):  # ten 100 ms gaps within one second
        t += 0.1
        times.append(t)
    play(monkeypatch, times)
    assert len(lines(caplog, "Late audio packet:")) == 1
    assert lines(caplog, "Late audio packets in") == [
        "Late audio packets in `Song`: 9, worst 100 ms."
    ]


def test_reset_timing_hides_a_short_pause(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="musicbot.player")
    clock = iter([0.0, 0.5])
    monkeypatch.setattr(player_mod, "time", SimpleNamespace(perf_counter=lambda: next(clock)))
    counter = SourcePlaybackCounter(FakeSource(2), label="Song")
    counter.read()
    counter.reset_timing()  # what MusicPlayer.resume() does
    counter.read()  # 500 ms later, but after a pause
    assert not lines(caplog, "Late audio packet")


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
