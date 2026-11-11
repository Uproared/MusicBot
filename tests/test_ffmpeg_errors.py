"""
S1: ffmpeg failures must reach the logs and the `after` callback.

These run the real ffmpeg through discord.py 2.7.1's real audio sources and
AudioPlayer thread; only the voice connection is a stand-in.
"""

import asyncio
import logging
import os
import threading
from collections import deque
from types import SimpleNamespace

import pytest
from discord import ClientException, FFmpegOpusAudio, FFmpegPCMAudio, PCMVolumeTransformer
from discord.player import AudioPlayer

from musicbot.exceptions import FFmpegError
from musicbot.player import FFmpegStderrReader, SourcePlaybackCounter


def make_counter(kind: str, path: str) -> SourcePlaybackCounter:
    """Build a source the way MusicPlayer._play does."""
    reader = FFmpegStderrReader(path)
    if kind == "opus":
        source = reader.create(
            FFmpegOpusAudio,
            path,
            bitrate=96,
            before_options="-nostdin",
            options="-vn -sn -dn",
        )
    else:
        source = PCMVolumeTransformer(
            reader.create(
                FFmpegPCMAudio, path, before_options="-nostdin", options="-vn -sn -dn"
            ),
            0.5,
        )
    return SourcePlaybackCounter(source, stderr=reader)


def read_to_end(counter: SourcePlaybackCounter) -> None:
    while counter.read():
        pass


@pytest.mark.parametrize("kind", ["opus", "pcm"])
@pytest.mark.parametrize("case", ["missing", "garbage"])
def test_failed_ffmpeg_is_reported(kind, case, media, caplog):
    caplog.set_level(logging.WARNING, logger="musicbot.player")
    counter = make_counter(kind, media[case])
    try:
        read_to_end(counter)
        err = counter._current_error
    finally:
        counter.cleanup()

    assert counter.frames == 0
    assert isinstance(err, FFmpegError)
    text = err.message % err.fmt_args
    assert "ffmpeg exited with code" in text
    # The error is posted to Discord: no directory paths in it.
    assert os.path.dirname(media[case]) not in text
    # ffmpeg's own stderr lines are logged at WARNING as they arrive.
    assert any(
        r.levelno == logging.WARNING and r.getMessage().startswith("ffmpeg: ")
        for r in caplog.records
    )


@pytest.mark.parametrize("kind", ["opus", "pcm"])
@pytest.mark.parametrize("case", ["good", "truncated"])
def test_successful_ffmpeg_has_no_error(kind, case, media):
    counter = make_counter(kind, media[case])
    try:
        read_to_end(counter)
        err = counter._current_error
    finally:
        counter.cleanup()

    assert counter.frames > 0
    assert err is None


class FakeVoiceClient:
    """Just enough of discord.VoiceClient for discord.py's AudioPlayer thread."""

    def __init__(self, loop):
        self.packets = 0
        self.ws = SimpleNamespace(speak=self._speak)
        self.client = SimpleNamespace(loop=loop)

    async def _speak(self, _state):
        return None

    def send_audio_packet(self, _data, encode=True):  # pylint: disable=unused-argument
        self.packets += 1

    def is_connected(self):
        return True


@pytest.mark.parametrize(
    "case,expect_error", [("missing", True), ("garbage", True), ("good", False)]
)
def test_audio_player_passes_error_to_after(case, expect_error, media, event_loop_thread):
    """The real integration point: AudioPlayer reads _current_error and calls after()."""
    results = []
    done = threading.Event()

    def after(error):
        results.append(error)
        done.set()

    client = FakeVoiceClient(event_loop_thread)
    player = AudioPlayer(make_counter("opus", media[case]), client, after=after)
    player.start()
    assert done.wait(20), "after() was never called"
    player.join(5)

    if expect_error:
        assert isinstance(results[0], FFmpegError)
    else:
        assert results[0] is None
        assert client.packets > 50  # ~2 s of 20 ms frames


class FakePlayVoiceClient(FakeVoiceClient):
    """Adds VoiceClient.play(): runs discord.py's real AudioPlayer thread."""

    def __init__(self, loop, connected=True, play_error=None):
        super().__init__(loop)
        self.channel = SimpleNamespace(bitrate=64000)
        self.audio_player = None
        self.connected = connected
        self.play_error = play_error

    def is_connected(self):
        return self.connected

    def play(self, source, *, after=None):
        if self.play_error:
            raise self.play_error
        self.audio_player = AudioPlayer(source, self, after=after)
        self.audio_player.start()

    def stop(self):
        if self.audio_player:
            self.audio_player.stop()


def run_play(path, loop, voice, *, use_opus=True, use_opus_probe=False, wait=True):
    """Drive MusicPlayer._play for one queued entry and collect its events."""
    from musicbot.player import MusicPlayer

    queued = SimpleNamespace(
        filename=path, probed_codec=None, probed_bitrate=0, start_time=0, playback_speed=1.0
    )

    class Playlist:
        def __init__(self):
            self.entries = deque()

        def on(self, *_args):
            return self

        def peek(self):
            return queued

        async def get_next_entry(self):
            return queued

    bot = SimpleNamespace(
        loop=loop,
        config=SimpleNamespace(
            default_volume=0.15,
            use_opus_audio=use_opus,
            use_opus_probe=use_opus_probe,
            save_videos=True,
        ),
    )
    player = MusicPlayer(bot, voice, Playlist())
    events = []
    player.on("error", lambda **kw: events.append(("error", kw["ex"])))
    player.on("finished-playing", lambda **kw: events.append(("finished", None)))

    async def run():
        await player._play()
        # The end of playback is handed to this loop (B1), so keep it running.
        for _ in range(400):
            if events or not wait:
                return
            await asyncio.sleep(0.05)

    loop.run_until_complete(run())
    if voice.audio_player:
        voice.audio_player.join(5)
    return player, events, queued


@pytest.mark.parametrize("use_opus", [True, False])
@pytest.mark.parametrize("case,expect_error", [("missing", True), ("good", False)])
def test_music_player_play_end_to_end(
    use_opus, case, expect_error, media, current_loop, event_loop_thread
):
    """MusicPlayer._play -> ffmpeg -> AudioPlayer -> _playback_finished -> events."""
    voice = FakePlayVoiceClient(event_loop_thread)
    _, events, _ = run_play(media[case], current_loop, voice, use_opus=use_opus)

    if expect_error:
        # With UseOpusAudio and no UseOpusProbe there's no probe any more, so a
        # missing file now reaches ffmpeg and is reported (it used to hit B3).
        assert events[0][0] == "error" and isinstance(events[0][1], FFmpegError)
    else:
        assert events == [("finished", None)]
        assert voice.packets > 50


def test_failed_probe_no_longer_crashes(media, current_loop, event_loop_thread):
    """B3: a failed probe returned (None, None) and min(bitrate, None) raised TypeError."""
    voice = FakePlayVoiceClient(event_loop_thread)
    _, events, queued = run_play(
        media["missing"], current_loop, voice, use_opus_probe=True
    )
    assert queued.probed_codec is None
    assert events[0][0] == "error" and isinstance(events[0][1], FFmpegError)


def test_source_start_failure_is_reported(media, current_loop, event_loop_thread):
    boom = ClientException("Already playing audio.")
    voice = FakePlayVoiceClient(event_loop_thread, play_error=boom)
    player, events, _ = run_play(media["good"], current_loop, voice, wait=False)
    assert events == [("error", boom)]
    assert player.is_stopped and player._source is None
    assert not player.playlist.entries


def test_source_start_without_voice_keeps_the_entry(media, current_loop, event_loop_thread):
    """No error event: on_player_error would start the next entry, which fails too."""
    voice = FakePlayVoiceClient(
        event_loop_thread, connected=False, play_error=ClientException("Not connected to voice.")
    )
    player, events, queued = run_play(media["good"], current_loop, voice, wait=False)
    assert events == []
    assert list(player.playlist.entries) == [queued]
    assert player.is_stopped


def test_summary_hides_urls_and_paths():
    path = os.path.join("home", "ubuntu", "bot", "audio_cache", "song.webm")
    reader = FFmpegStderrReader(path)
    reader._tail.append(f"Error opening input file {path}.")
    assert reader.summary() == "Error opening input file song.webm."

    url = "https://rr1---sn-x.googlevideo.com/videoplayback?ip=203.0.113.9&sig=abc"
    reader = FFmpegStderrReader(url)
    reader._tail.append(f"[https @ 0x1] HTTP error 403 Forbidden\n{url}: Server returned 403")
    text = reader.summary()
    assert "203.0.113.9" not in text and "googlevideo" not in text
    assert "<url>" in text


def test_summary_prefers_real_errors_over_known_noise():
    reader = FFmpegStderrReader("x.mp3")
    reader._tail.append("Invalid data found when processing input")
    reader._tail.append("Last message repeated 3 times")
    assert reader.summary() == "Invalid data found when processing input"


def test_create_closes_pipe_when_source_fails():
    seen = {}

    class Boom:
        def __init__(self, *_args, stderr=None, **_kwargs):
            seen["stderr"] = stderr
            raise RuntimeError("ffmpeg was not found")

    with pytest.raises(RuntimeError):
        FFmpegStderrReader("x").create(Boom, "x")
    assert seen["stderr"].closed


def test_source_start_failure_is_marked_as_starting(
    media, current_loop, event_loop_thread, monkeypatch
):
    """Review #1: on_player_error must be able to tell a start failure apart."""
    from musicbot.player import MusicPlayer

    seen = []
    real_emit = MusicPlayer.emit

    def emit(self, event, *args, **kwargs):
        if event == "error":
            seen.append(kwargs.get("starting"))
        return real_emit(self, event, *args, **kwargs)

    monkeypatch.setattr(MusicPlayer, "emit", emit)
    voice = FakePlayVoiceClient(
        event_loop_thread, play_error=ClientException("ffmpeg was not found.")
    )
    run_play(media["good"], current_loop, voice, wait=False)
    assert seen == [True]
