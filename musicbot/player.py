import asyncio
import functools
import json
import logging
import os
import re
import subprocess
from collections import deque
from enum import Enum
from threading import Thread
from typing import (
    IO,
    TYPE_CHECKING,
    Any,
    Deque,
    Dict,
    List,
    Optional,
    Type,
    TypeVar,
    Union,
)

from discord import (
    AudioSource,
    FFmpegOpusAudio,
    FFmpegPCMAudio,
    PCMVolumeTransformer,
    VoiceClient,
)

from .constants import MUSICBOT_VOICE_MAX_KBITRATE
from .constructs import Serializable, Serializer, SkipState
from .entry import LocalFilePlaylistEntry, StreamPlaylistEntry, URLPlaylistEntry
from .exceptions import FFmpegError
from .lib.event_emitter import EventEmitter

if TYPE_CHECKING:
    from .bot import MusicBot
    from .playlist import Playlist

# Type alias
EntryTypes = Union[URLPlaylistEntry, StreamPlaylistEntry, LocalFilePlaylistEntry]
FFmpegSources = Union[PCMVolumeTransformer[FFmpegPCMAudio], FFmpegOpusAudio]
FFmpegAudioT = TypeVar("FFmpegAudioT", FFmpegOpusAudio, FFmpegPCMAudio)

log = logging.getLogger(__name__)


class FFmpegStderrReader:
    """
    Collect an ffmpeg process's stderr through a pipe MusicBot owns.

    Each line is logged as it arrives and the last few are kept for the error
    message. discord.py's own stderr handling isn't used: it hands data over
    only in 8 KB blocks, so the final error line may arrive after playback has
    already been reported as finished.
    """

    # Warnings ffmpeg prints for media that still plays. Logged at the FFMPEG
    # level instead of WARNING.
    KNOWN_NOISE = (
        "Header missing",
        "Estimating duration from birate, this may be inaccurate",
        "Using AVStream.codec to pass codec parameters to muxers is deprecated, use AVStream.codecpar instead.",
        "Application provided invalid, non monotonically increasing dts to muxer in stream",
        "Last message repeated",
        "Failed to send close message",
        "decode_band_types: Input buffer exhausted before END element found",
    )

    def __init__(self, input_name: str) -> None:
        """
        :param: input_name:  The file or URL given to ffmpeg. It is shortened
            in error messages so paths and stream URLs aren't posted to Discord.
        """
        self._input_name = input_name
        self._tail: Deque[str] = deque(maxlen=8)
        self._thread: Optional[Thread] = None

    def create(
        self, source_cls: Type[FFmpegAudioT], *args: Any, **kwargs: Any
    ) -> FFmpegAudioT:
        """
        Create an ffmpeg audio source with its stderr sent to this reader.
        """
        read_fd, write_fd = os.pipe()
        try:
            # ffmpeg gets its own copy of the write end; ours must be closed so
            # the reader sees end-of-file when ffmpeg exits.
            with open(write_fd, "wb") as write_end:
                source = source_cls(*args, stderr=write_end, **kwargs)
        except BaseException:
            os.close(read_fd)
            raise

        self._thread = Thread(
            target=self._read_lines,
            args=(open(read_fd, "rb"),),  # pylint: disable=consider-using-with
            name="MB_FFmpegStdErrReader",
            daemon=True,
        )
        self._thread.start()
        return source

    def _read_lines(self, read_end: IO[bytes]) -> None:
        """Log ffmpeg's stderr line by line until ffmpeg exits."""
        try:
            with read_end:
                for raw in read_end:
                    line = raw.decode("utf8", errors="replace").strip()
                    if not line:
                        continue
                    self._tail.append(line)
                    if any(msg in line for msg in self.KNOWN_NOISE):
                        log.ffmpeg("ffmpeg: %s", line)  # type: ignore[attr-defined]
                    else:
                        log.warning("ffmpeg: %s", line)
        except (OSError, ValueError):
            log.debug("Stopped reading ffmpeg stderr.", exc_info=True)

    def wait_done(self, timeout: float) -> None:
        """Wait for the reader to see ffmpeg's last output."""
        if self._thread:
            self._thread.join(timeout)

    def summary(self) -> str:
        """The last meaningful stderr line, safe to show in Discord."""
        lines = [x for x in self._tail if not any(n in x for n in self.KNOWN_NOISE)]
        if not lines:
            lines = list(self._tail)
        if not lines:
            return ""
        # URLs first: a stream input is a URL, and YouTube's include the server IP.
        text = re.sub(r"https?://\S+", "<url>", lines[-1])
        if self._input_name:
            short = os.path.basename(self._input_name) or "<input>"
            text = text.replace(self._input_name, short)
        return text[:300]


class MusicPlayerState(Enum):
    STOPPED = 0  # When the player isn't playing anything
    PLAYING = 1  # The player is actively playing music.
    PAUSED = 2  # The player is paused on a song.
    WAITING = (
        3  # The player has finished its song but is still downloading the next one
    )
    DEAD = 4  # The player has been killed.

    def __str__(self) -> str:
        return self.name


class SourcePlaybackCounter(AudioSource):
    def __init__(
        self,
        source: FFmpegSources,
        start_time: float = 0,
        playback_speed: float = 1.0,
        stderr: Optional[FFmpegStderrReader] = None,
    ) -> None:
        """
        Manage playback source and attempt to count progress frames used
        to measure playback progress.

        :param: start_time:  A time in seconds that was used in ffmpeg -ss flag.
        :param: stderr:  The reader collecting this source's ffmpeg stderr.
        """
        # NOTE: PCMVolumeTransformer will let you set any crazy value.
        # But internally it limits between 0 and 2.0.
        self._source = source
        self._num_reads: int = 0
        self._start_time: float = start_time
        self._playback_speed: float = playback_speed
        self._is_opus_audio: bool = isinstance(source, FFmpegOpusAudio)
        self._stderr = stderr
        self._error: Optional[Exception] = None

    def is_opus(self) -> bool:
        return self._is_opus_audio

    def read(self) -> bytes:
        res = self._source.read()
        if res:
            self._num_reads += 1
        elif self._error is None:
            self._error = self._check_ffmpeg_exit()
        return res

    @property
    def _current_error(self) -> Optional[Exception]:
        """
        discord.py's AudioPlayer reads this when read() returns no data and
        passes it to the `after` callback (MusicPlayer._playback_finished).
        """
        return self._error

    def _check_ffmpeg_exit(self) -> Optional[Exception]:
        """
        Called when the source runs out of data: report a failed ffmpeg run.

        discord.py 2.7.1 checks the exit code only at this same moment, before
        ffmpeg has usually finished exiting, so failures looked like a normal
        end of track. Wait briefly for the real exit code instead.
        """
        ffmpeg = self._source
        if isinstance(ffmpeg, PCMVolumeTransformer):
            ffmpeg = ffmpeg.original
        proc = getattr(ffmpeg, "_process", None)
        if not isinstance(proc, subprocess.Popen):
            return None

        try:
            code = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            log.warning("ffmpeg is still running after its output ended.")
            return None
        if code == 0:
            return None

        detail = ""
        if self._stderr:
            self._stderr.wait_done(timeout=2)
            detail = self._stderr.summary()
        if detail:
            return FFmpegError(
                "ffmpeg exited with code %(code)s:  %(detail)s",
                fmt_args={"code": code, "detail": detail},
            )
        return FFmpegError(
            "ffmpeg exited with code %(code)s.", fmt_args={"code": code}
        )

    def cleanup(self) -> None:
        log.noise(  # type: ignore[attr-defined]
            "Cleanup got called on the audio source:  %r", self
        )
        self._source.cleanup()

    @property
    def frames(self) -> int:
        """
        Number of read frames since this source was opened.
        This is not the total playback time.
        """
        return self._num_reads

    @property
    def session_progress(self) -> float:
        """
        Like progress but only counts frames from this session.
        Adjusts the estimated time by playback speed.
        """
        return (self._num_reads * 0.02) * self._playback_speed

    @property
    def progress(self) -> float:
        """Get an approximate playback progress time."""
        return self._start_time + self.session_progress


class MusicPlayer(EventEmitter, Serializable):
    def __init__(
        self,
        bot: "MusicBot",
        voice_client: VoiceClient,
        playlist: "Playlist",
    ):
        """
        Manage a MusicPlayer with all its bits and bolts.

        :param: bot:  A MusicBot discord client instance.
        :param: voice_client:  a discord.VoiceClient object used for playback.
        :param: playlist:  a collection of playable entries to be played.
        """
        super().__init__()
        self.bot: MusicBot = bot
        self.loop: asyncio.AbstractEventLoop = bot.loop
        self.loopqueue: bool = False
        self.repeatsong: bool = False
        self.voice_client: VoiceClient = voice_client
        self.playlist: Playlist = playlist
        self.autoplaylist: List[str] = []
        self.state: MusicPlayerState = MusicPlayerState.STOPPED
        self.skip_state: SkipState = SkipState()
        self.karaoke_mode: bool = False
        self.guild_or_net_unavailable: bool = False
        self.paused_auto: bool = False

        self._volume = bot.config.default_volume
        self._play_lock = asyncio.Lock()
        self._current_player: Optional[VoiceClient] = None
        self._current_entry: Optional[EntryTypes] = None

        self._source: Optional[SourcePlaybackCounter] = None

        self.playlist.on("entry-added", self.on_entry_added)
        self.playlist.on("entry-failed", self.on_entry_failed)

    @property
    def volume(self) -> float:
        """Get the volume level as last set by config or command."""
        return self._volume

    @volume.setter
    def volume(self, value: float) -> None:
        """
        Set volume to the given `value` and immediately apply it to any
        active playback source.
        """
        self._volume = value
        if self._source:
            if isinstance(self._source._source, PCMVolumeTransformer):
                self._source._source.volume = value
            # if isinstance(self._source._source, FFmpegOpusAudio):

    def on_entry_added(
        self, playlist: "Playlist", entry: EntryTypes, defer_serialize: bool = False
    ) -> None:
        """
        Event dispatched by Playlist when an entry is added to the queue.
        """
        self.emit(
            "entry-added",
            player=self,
            playlist=playlist,
            entry=entry,
            defer_serialize=defer_serialize,
        )

    def on_entry_failed(self, entry: EntryTypes, error: Exception) -> None:
        """
        Event dispatched by Playlist when an entry failed to ready or play.
        """
        self.emit("error", player=self, entry=entry, ex=error)

    def skip(self) -> None:
        """Skip the current playing entry but just killing playback."""
        log.noise(  # type: ignore[attr-defined]
            "MusicPlayer.skip() is called:  %s", repr(self)
        )
        self._kill_current_player()

    def stop(self) -> None:
        """
        Immediately halt playback, killing current player source, setting
        state to stopped and emitting an event.
        """
        log.noise(  # type: ignore[attr-defined]
            "MusicPlayer.stop() is called:  %s", repr(self)
        )
        self.state = MusicPlayerState.STOPPED
        self._kill_current_player()

        self.emit("stop", player=self)

    def resume(self) -> None:
        """
        Resume the player audio playback if it was paused and we have a
        VoiceClient playback source.
        If MusicPlayer was paused but the VoiceClient player is missing,
        do something odd and set state to playing but kill the player...
        """
        if self.guild_or_net_unavailable:
            log.warning("Guild or network unavailable, cannot resume playback.")
            return

        log.noise(  # type: ignore[attr-defined]
            "MusicPlayer.resume() is called:  %s", repr(self)
        )
        if self.is_paused and self._current_player:
            self._current_player.resume()
            self.state = MusicPlayerState.PLAYING
            self.emit("resume", player=self, entry=self.current_entry)
            return

        if self.is_paused and not self._current_player:
            self.state = MusicPlayerState.PLAYING
            self._kill_current_player()
            return

        raise ValueError(f"Cannot resume playback from state {self.state}")

    def pause(self) -> None:
        """
        Suspend player audio playback and emit an event, if the player was playing.
        """
        log.noise(  # type: ignore[attr-defined]
            "MusicPlayer.pause() is called:  %s", repr(self)
        )
        if self.is_playing:
            self.state = MusicPlayerState.PAUSED

            if self._current_player:
                self._current_player.pause()

            self.emit("pause", player=self, entry=self.current_entry)
            return

        if self.is_paused:
            return

        raise ValueError(f"Cannot pause a MusicPlayer in state {self.state}")

    def kill(self) -> None:
        """
        Set the state of the bot to Dead, clear all events and playlists,
        then kill the current VoiceClient source player.
        """
        log.noise(  # type: ignore[attr-defined]
            "MusicPlayer.kill() is called:  %s", repr(self)
        )
        self.state = MusicPlayerState.DEAD
        self.playlist.clear()
        self._events.clear()
        self._kill_current_player()

    def _playback_finished(
        self,
        error: Optional[Exception] = None,
        source: Optional[SourcePlaybackCounter] = None,
    ) -> None:
        """
        Called by discord.py on its audio thread when playback ends or fails.
        asyncio isn't thread-safe, so the work is handed to the event loop
        (B1: tasks created from this thread could sit unscheduled).

        :param: error:  An exception, if any, raised by playback.
        :param: source:  The source that finished, bound in _play().
        """
        try:
            self.loop.call_soon_threadsafe(self._on_playback_finished, error, source)
        except RuntimeError:
            log.debug("Playback finished after the event loop closed.")

    def _on_playback_finished(
        self,
        error: Optional[Exception] = None,
        source: Optional[SourcePlaybackCounter] = None,
    ) -> None:
        """
        Runs on the event loop after playback has finished or failed.
        This function is responsible tidying the queue post-playback,
        propagating player error or finished-playing events, and
        triggering the media file cleanup task.

        :param: error:  An exception, if any, raised by playback.
        :param: source:  The source that finished. If another source has
            replaced it since, this call is stale and ignored.
        """
        if source is not None and source is not self._source:
            log.debug("Ignoring the end of a source that was already replaced.")
            return

        entry = self._current_entry
        if entry is None:
            log.debug("Playback finished, but _current_entry is None.")
            return

        # Re-queue for repeat/loop, except when:
        # - playback failed, so a broken track can't loop forever;
        # - seek, speed or volume already put this entry back at the head
        #   to restart it, which would otherwise queue it twice.
        requeued = bool(self.playlist.entries) and self.playlist.entries[0] is entry
        if not error and not requeued:
            if self.repeatsong:
                self.playlist.entries.appendleft(entry)
            elif self.loopqueue:
                self.playlist.entries.append(entry)

        # TODO: investigate if this is cruft code or not.
        if self._current_player:
            if hasattr(self._current_player, "after"):
                self._current_player.after = None
            self._kill_current_player()

        self._current_entry = None
        self._source = None
        self.stop()

        # Delete the media file if not keeping them, whether or not playback
        # worked. It is kept while the same file is still queued.
        if not self.bot.config.save_videos and entry:
            self.bot.create_task(
                self._handle_file_cleanup(entry), name="MB_CacheCleanup"
            )

        # if an error was set, report it and return...
        if error:
            self.emit("error", player=self, entry=entry, ex=error)
            return

        # finally, tell the rest of MusicBot that playback is done.
        self.emit("finished-playing", player=self, entry=entry)

    def _kill_current_player(self) -> bool:
        """
        If there is a current player source, attempt to stop it, then
        say "Garbage day!" and set it to None anyway.
        """
        if self._current_player:
            try:
                self._current_player.stop()
            except OSError:
                log.noise(  # type: ignore[attr-defined]
                    "Possible Warning from kill_current_player()", exc_info=True
                )

            self._current_player = None
            return True

        return False

    def play(self, _continue: bool = False) -> None:
        """
        Immediately try to gracefully play the next entry in the queue.
        If there is already an entry, but player is paused, playback will
        resume instead of playing a new entry.
        If the player is dead, this will silently return.

        :param: _continue:  Force a player that is not dead or stopped to
            start a new playback source anyway.
        """
        log.noise(  # type: ignore[attr-defined]
            "MusicPlayer.play() is called:  %s", repr(self)
        )
        self.bot.create_task(self._play(_continue=_continue), name="MB_Play")

    async def _play(self, _continue: bool = False) -> None:
        """
        Plays the next entry from the playlist, or resumes playback of the current entry if paused.
        """
        if self.is_dead:
            log.voicedebug(  # type: ignore[attr-defined]
                "MusicPlayer is dead, cannot play."
            )
            return

        if self.guild_or_net_unavailable:
            log.warning("Guild or network unavailable, cannot start playback.")
            return

        if self.is_paused and self._current_player:
            log.voicedebug(  # type: ignore[attr-defined]
                "MusicPlayer was previously paused, resuming current player."
            )
            return self.resume()

        if self._play_lock.locked():
            log.voicedebug(  # type: ignore[attr-defined]
                "MusicPlayer already locked for playback, this call is ignored."
            )
            return

        async with self._play_lock:
            if self.is_stopped or _continue:
                # Get the entry before we try to ready it, so it can be passed to error callbacks.
                entry_up_next = self.playlist.peek()
                try:
                    entry = await self.playlist.get_next_entry()
                except IndexError as e:
                    log.warning("Failed to get next entry.", exc_info=e)
                    self.emit("error", player=self, entry=entry_up_next, ex=e)
                    entry = None
                except Exception as e:  # pylint: disable=broad-exception-caught
                    log.warning("Failed to process entry for playback.", exc_info=e)
                    self.emit("error", player=self, entry=entry_up_next, ex=e)
                    entry = None

                # If nothing left to play, transition to the stopped state.
                if not entry:
                    self.stop()
                    return

                # In-case there was a player, kill it. RIP.
                self._kill_current_player()

                boptions = "-nostdin"
                aoptions = "-vn -sn -dn"  # "-b:a 192k"
                if isinstance(entry, (URLPlaylistEntry, LocalFilePlaylistEntry)):
                    if self.bot.config.use_opus_audio:
                        entry.set_audio_filter("volume", f"{self.volume:.2f}")
                    # check for after-options, usually filters for speed and EQ.
                    if entry.aoptions and not self.bot.config.use_opus_probe:
                        aoptions += f" {entry.aoptions}"
                    # check for before options, currently just -ss here.
                    if entry.boptions:
                        boptions += f" {entry.boptions}"
                elif (
                    isinstance(entry, StreamPlaylistEntry)
                    and self.bot.config.use_opus_audio
                ):
                    entry.set_audio_filter("volume", f"{self.volume:.2f}")
                    aoptions += f" {entry.get_audio_filters()}"

                counter: Optional[SourcePlaybackCounter] = None
                try:
                    stderr_reader = FFmpegStderrReader(entry.filename)

                    if self.bot.config.use_opus_probe or self.bot.config.use_opus_audio:
                        # Note: ffmpeg filters require encoding to work.
                        # So copy codec will not allow speed, volume or other filters.
                        # Probe only when its codec is used (UseOpusProbe). discord.py
                        # never reports less than 512 kbps, so the probed bitrate can't
                        # lower the result below; without UseOpusProbe the probe only
                        # cost an ffprobe run per track, and a failed one crashed (B3).
                        if self.bot.config.use_opus_probe and not entry.probed_codec:
                            codec, bitrate = await FFmpegOpusAudio.probe(
                                entry.filename,
                                method="native",
                            )
                            log.everything(  # type: ignore[attr-defined]
                                "Probed codec and bitrate: %(name)s at %(rate)s kbps",
                                {"name": codec, "rate": bitrate},
                            )
                            # If the probe returned opus, libopus, or copy, copy is used.
                            entry.probed_codec = codec
                            if bitrate:
                                entry.probed_bitrate = bitrate

                        # Determine bitrate to use based on probe, channel setting, and bot limit.
                        vc_kbitrate = self.voice_client.channel.bitrate / 1000
                        kbitrate = min(vc_kbitrate, entry.probed_bitrate or vc_kbitrate)
                        kbitrate = min(kbitrate, MUSICBOT_VOICE_MAX_KBITRATE)
                        source: FFmpegSources = stderr_reader.create(
                            FFmpegOpusAudio,
                            entry.filename,
                            codec=entry.probed_codec,
                            bitrate=kbitrate,
                            before_options=boptions,
                            options=aoptions,
                        )
                    else:
                        source = PCMVolumeTransformer(
                            stderr_reader.create(
                                FFmpegPCMAudio,
                                entry.filename,
                                before_options=boptions,
                                options=aoptions,
                            ),
                            self.volume,
                        )

                    counter = SourcePlaybackCounter(
                        source,
                        start_time=entry.start_time,
                        playback_speed=entry.playback_speed,
                        stderr=stderr_reader,
                    )
                    self._source = counter
                    log.ffmpeg(  # type: ignore[attr-defined]
                        "Creating player with options: ffmpeg %(before)s -i %(input)s %(after)s",
                        {"before": boptions, "input": entry.filename, "after": aoptions},
                    )
                    log.voicedebug(  # type: ignore[attr-defined]
                        "Playing %(source)r using %(client)r",
                        {"source": self._source, "client": self.voice_client},
                    )
                    self.voice_client.play(
                        counter,
                        after=functools.partial(self._playback_finished, source=counter),
                    )
                except Exception as e:  # pylint: disable=broad-exception-caught
                    # Report the failure so on_player_error moves on to the next entry,
                    # instead of dropping this one silently (B3).
                    if counter:
                        counter.cleanup()
                    self._source = None
                    self.stop()
                    if not self.voice_client.is_connected():
                        # Without voice every queued entry would fail in turn; keep this one.
                        log.warning("Voice is not connected, cannot start playback.")
                        self.playlist.entries.appendleft(entry)
                        return
                    self.emit("error", player=self, entry=entry, ex=e)
                    return

                self._current_player = self.voice_client

                # I need to add ytdl hooks
                self.state = MusicPlayerState.PLAYING
                self._current_entry = entry

                self.emit("play", player=self, entry=entry)

    async def _handle_file_cleanup(self, entry: EntryTypes) -> None:
        """
        A helper used to clean up media files via call-later, when file
        cache is not enabled.
        """
        if not isinstance(entry, StreamPlaylistEntry):
            if any(entry.filename == e.filename for e in self.playlist.entries):
                log.debug(
                    "Skipping deletion of '%s', found song in queue",
                    entry.filename,
                )
            else:
                log.debug("Deleting file:  %s", os.path.relpath(entry.filename))
                filename = entry.filename
                for _ in range(3):
                    try:
                        os.unlink(filename)
                        log.debug("File deleted:  %s", filename)
                        break
                    except PermissionError as e:
                        # File in use (Windows only; it reports winerror 32 with
                        # errno 13). ffmpeg may not have exited yet, so wait a bit.
                        if 32 in (e.errno, getattr(e, "winerror", None)):
                            log.debug("File is still in use, retrying:  %s", filename)
                            await asyncio.sleep(1)
                        else:
                            log.warning(
                                "Cannot delete file due to a permission error.",
                                exc_info=True,
                            )
                    except FileNotFoundError:
                        # Expected when playback failed because the file was missing.
                        log.debug("File was already gone:  %s", filename)
                        break
                    except (OSError, IsADirectoryError):
                        log.warning(
                            "Error while trying to delete file.",
                            exc_info=True,
                        )
                        break
                else:
                    log.warning("Could not delete file, giving up:  %s", filename)

    def __json__(self) -> Dict[str, Any]:
        return self._enclose_json(
            {
                "current_entry": {
                    "entry": self.current_entry,
                    "progress": self.progress if self.progress > 1 else None,
                },
                "entries": self.playlist,
            }
        )

    @classmethod
    def _deserialize(
        cls,
        raw_json: Dict[str, Any],
        bot: Optional["MusicBot"] = None,
        voice_client: Optional[VoiceClient] = None,
        playlist: Optional["Playlist"] = None,
        **kwargs: Any,
    ) -> "MusicPlayer":
        assert bot is not None, cls._bad("bot")
        assert voice_client is not None, cls._bad("voice_client")
        assert playlist is not None, cls._bad("playlist")

        player = cls(bot, voice_client, playlist)

        data_pl = raw_json.get("entries")
        if data_pl and data_pl.entries:
            player.playlist.entries = data_pl.entries

        current_entry_data = raw_json["current_entry"]
        if current_entry_data["entry"]:
            entry = current_entry_data["entry"]
            progress = current_entry_data["progress"]
            if progress and isinstance(
                entry, (URLPlaylistEntry, LocalFilePlaylistEntry)
            ):
                entry.set_start_time(progress)

            # Check if user disabled autoplaylist before re-adding.
            if (
                entry.from_auto_playlist and bot.config.auto_playlist
            ) or not entry.from_auto_playlist:
                player.playlist.entries.appendleft(entry)
        return player

    @classmethod
    def from_json(
        cls,
        raw_json: str,
        bot: "MusicBot",  # pylint: disable=unused-argument
        voice_client: VoiceClient,  # pylint: disable=unused-argument
        playlist: "Playlist",  # pylint: disable=unused-argument
    ) -> Optional["MusicPlayer"]:
        """
        Create a MusicPlayer instance from serialized `raw_json` string data.
        The remaining arguments are made available to the MusicPlayer
        and other serialized instances via call frame inspection.
        """
        try:
            obj = json.loads(raw_json, object_hook=Serializer.deserialize)
            if isinstance(obj, MusicPlayer):
                return obj
            log.error(
                "Deserialize returned an object that is not a MusicPlayer:  %s",
                type(obj),
            )
            return None
        except json.JSONDecodeError:
            log.exception("Failed to deserialize player")
            return None

    @property
    def current_entry(self) -> Optional[EntryTypes]:
        """Get the currently playing entry if there is one."""
        return self._current_entry

    @property
    def is_playing(self) -> bool:
        """Test if MusicPlayer is in a playing state"""
        return self.state == MusicPlayerState.PLAYING

    @property
    def is_paused(self) -> bool:
        """Test if MusicPlayer is in a paused state"""
        return self.state == MusicPlayerState.PAUSED

    @property
    def is_stopped(self) -> bool:
        """Test if MusicPlayer is in a stopped state"""
        return self.state == MusicPlayerState.STOPPED

    @property
    def is_dead(self) -> bool:
        """Test if MusicPlayer is in a dead state"""
        return self.state == MusicPlayerState.DEAD

    @property
    def progress(self) -> float:
        """
        Return a progress value for the media playback.
        """
        if self._source:
            return self._source.progress
        return 0

    @property
    def session_progress(self) -> float:
        """
        Return the estimated playback time of the current playback source.
        Like progress, but does not include any start-time if one was used.
        """
        if self._source:
            return self._source.session_progress
        return 0


# if redistributing ffmpeg is an issue, it can be downloaded from here:
#  - http://ffmpeg.zeranoe.com/builds/win32/static/ffmpeg-latest-win32-static.7z
#  - http://ffmpeg.zeranoe.com/builds/win64/static/ffmpeg-latest-win64-static.7z
#
# Extracting bin/ffmpeg.exe, bin/ffplay.exe, and bin/ffprobe.exe should be fine
# However, the files are in 7z format so meh
# I don't know if we can even do this for the user, at most we open it in the browser
# I can't imagine the user is so incompetent that they can't pull 3 files out of it...
# ...
# ...right?

# Get duration with ffprobe
#   ffprobe.exe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 -sexagesimal filename.mp3
# This is also how I fix the format checking issue for now
# ffprobe -v quiet -print_format json -show_format stream

# Normalization filter
# -af dynaudnorm
