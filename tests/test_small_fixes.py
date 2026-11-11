"""
Batch 8: small correctness fixes (B5, B11, B12).

Each test drives the real handler with small fakes; nothing connects to
Discord or the network.
"""

import asyncio
import logging
import pathlib
import subprocess
import sys
from types import SimpleNamespace

import discord
import pytest

from musicbot import exceptions
from musicbot.entry import URLPlaylistEntry

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture
def bot_cls():
    from musicbot.bot import MusicBot
    from musicbot.i18n import I18n

    I18n()
    return MusicBot


async def _noop(*_args, **_kwargs):
    return None


# --- B5: skip-when-absent ----------------------------------------------------


def test_skip_when_absent_skips_and_tells_the_user(bot_cls):
    sent, skipped = [], []

    async def safe_send_message(dest, content):
        sent.append(content)

    guild = SimpleNamespace(id=1)
    fake = SimpleNamespace(
        server_data={1: SimpleNamespace(lang_code="", guild_id=1, last_np_msg=None)},
        config=SimpleNamespace(
            write_current_song=False,
            enable_queue_history_global=False,
            enable_queue_history_guilds=False,
            now_playing_mentions=False,
            dm_nowplaying=True,
        ),
        permissions=SimpleNamespace(for_user=lambda _u: SimpleNamespace(skip_when_absent=True)),
        _handle_guild_auto_pause=_noop,
        reset_player_inactivity=_noop,
        update_now_playing_status=_noop,
        serialize_queue=_noop,
        safe_send_message=safe_send_message,
    )
    entry = SimpleNamespace(
        channel=object(),
        author=SimpleNamespace(name="absentee"),
        title="song",
        url="u",
        from_auto_playlist=False,
    )
    player = SimpleNamespace(
        voice_client=SimpleNamespace(
            channel=SimpleNamespace(guild=guild, members=[], name="General"),
            guild=guild,
        ),
        skip_state=SimpleNamespace(reset=lambda: None),
        current_entry=entry,
        skip=lambda: skipped.append(True),
    )

    # Before: KeyError('author') here, so the song was never skipped.
    asyncio.run(bot_cls.on_player_play(fake, player=player, entry=entry))
    assert skipped == [True]
    assert "absentee" in sent[0].description


# --- B11 and the blocked-user log line: on_message ---------------------------


def _message_bot(**config):
    defaults = dict(command_prefix="!", commands_via_mention=False)
    defaults.update(config)
    return SimpleNamespace(
        user=SimpleNamespace(id=42),
        config=SimpleNamespace(**defaults),
        wait_until_ready=_noop,
        server_data={},
    )


@pytest.mark.parametrize(
    "content, mentions, parsed",
    [
        (".skip", False, False),  # another bot's prefix
        ("-play x", False, False),
        ("!skip", False, True),
        ("<@42> skip", True, True),
        ("<@42> skip", False, False),
        ("hello there", True, False),
    ],
)
def test_only_prefixed_messages_are_commands(bot_cls, caplog, content, mentions, parsed):
    fake = _message_bot(commands_via_mention=mentions)
    # The author is the bot itself: a message that gets past the prefix
    # check is then dropped with "Ignoring command from myself".
    message = SimpleNamespace(channel=SimpleNamespace(guild=None), content=content, author=fake.user)
    asyncio.run(bot_cls.on_message(fake, message))
    reached = any("Ignoring command from myself" in r.getMessage() for r in caplog.records)
    assert reached is parsed


class FakeGuildChannel(discord.abc.GuildChannel):
    def __init__(self, guild):
        self.guild = guild


def test_blocked_user_warning_can_be_formatted(bot_cls, caplog):
    guild = SimpleNamespace(id=1)
    fake = _message_bot(
        bound_channels=set(),
        owner_id=1,
        bot_exception_ids=set(),
        user_blocklist=SimpleNamespace(is_blocked=lambda _u: True),
    )
    fake.server_data = {1: SimpleNamespace(command_prefix="!")}
    fake.cmd_play = _noop
    message = SimpleNamespace(
        channel=FakeGuildChannel(guild),
        guild=guild,
        content="!play x",
        author=SimpleNamespace(id=7, bot=False),
    )
    asyncio.run(bot_cls.on_message(fake, message))
    record = next(r for r in caplog.records if r.msg.startswith("User in block list"))
    # Before: "comand" in the args, so formatting raised KeyError('command').
    assert "tried command: play" in record.getMessage()


# --- B12: tasks and timers ----------------------------------------------------


def test_cancelled_task_leaves_the_pool_quietly(bot_cls, current_loop, caplog):
    fake = SimpleNamespace(loop=current_loop, task_pool=set())
    bot_cls.create_task(fake, asyncio.sleep(10))
    (task,) = fake.task_pool
    task.cancel()
    current_loop.run_until_complete(asyncio.sleep(0.01))
    # Before: exception() raised CancelledError in the callback, so the task
    # stayed in the pool and asyncio logged "Exception in callback".
    assert not fake.task_pool
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_inactivity_timeout_leaves_no_waiting_task(bot_cls):
    from musicbot.constructs import GuildAsyncEvent

    expired = []

    async def on_inactivity_timeout_expired(channel):
        expired.append(channel)

    async def run():
        event = GuildAsyncEvent()
        fake = SimpleNamespace(
            logout_called=False,
            config=SimpleNamespace(leave_player_inactive_for=0.01, autojoin_channels=set()),
            server_data={1: SimpleNamespace(get_event=lambda _name: event)},
            on_inactivity_timeout_expired=on_inactivity_timeout_expired,
        )
        channel = SimpleNamespace(id=5, name="General", guild=SimpleNamespace(id=1, name="g"))
        player = SimpleNamespace(
            voice_client=SimpleNamespace(channel=channel, is_connected=lambda: True),
            is_playing=False,
        )
        await bot_cls.handle_player_inactivity(fake, player)
        await asyncio.sleep(0)
        return [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]

    leftover = asyncio.run(run())
    assert len(expired) == 1
    # Before: sane_wait_for left the event.wait() task pending after the timeout.
    assert leftover == []


class FakePlaylist(list):
    @property
    def entries(self):
        return self


def test_leave_after_queue_empty_stops_there(bot_cls):
    left = []

    async def disconnect_voice_client(guild):
        left.append(guild)

    async def run():
        fake = SimpleNamespace(
            loop=asyncio.get_running_loop(),
            logout_called=False,
            config=SimpleNamespace(
                enable_queue_history_global=False,
                enable_queue_history_guilds=False,
                delete_nowplaying=False,
                leave_after_queue_empty=True,
                auto_playlist=True,
            ),
            server_data={1: SimpleNamespace(current_playing_url="", autoplaylist=[])},
            disconnect_voice_client=disconnect_voice_client,
        )
        player = SimpleNamespace(
            voice_client=SimpleNamespace(guild=SimpleNamespace(id=1), is_connected=lambda: True),
            playlist=FakePlaylist(),
            current_entry=None,
            autoplaylist=[],
        )
        await bot_cls.on_player_finished_playing(fake, player)
        return fake

    fake = asyncio.run(run())
    assert len(left) == 1
    # Before: it carried on into the autoplaylist code on the killed player,
    # which here (empty autoplaylist) switched the autoplaylist off.
    assert fake.config.auto_playlist is True


# --- B12: small input and lookup fixes ------------------------------------------


def test_text_html_stream_keeps_the_requester():
    from musicbot.playlist import Playlist

    calls = []

    async def add_stream_from_info(info, **kwargs):
        calls.append(kwargs)
        return "stream", 1

    fake = SimpleNamespace(add_stream_from_info=add_stream_from_info)
    info = SimpleNamespace(
        ytdl_type="video",
        is_stream=False,
        extractor="generic",
        http_header=lambda _key, _default=None: "text/html; charset=utf-8",
        url="http://radio.example/live",
        input_subject="http://radio.example/live",
    )
    author, channel = object(), object()
    asyncio.run(Playlist.add_entry_from_info(fake, info, author=author, channel=channel))
    # Before: only head was passed, so it looked like an autoplaylist entry.
    assert calls[0]["author"] is author
    assert calls[0]["channel"] is channel


def test_category_rename_is_logged_without_error(bot_cls, caplog):
    caplog.set_level(logging.INFO)
    before = SimpleNamespace(name="Old")  # like CategoryChannel: no slowmode_delay
    after = SimpleNamespace(name="New")
    asyncio.run(bot_cls.on_guild_channel_update(SimpleNamespace(), before, after))
    assert any("name = New" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("value", ["nan", "NaN", "inf", "0.1", "101"])
def test_speed_rejects_nan_and_out_of_range(bot_cls, value):
    entry = URLPlaylistEntry.__new__(URLPlaylistEntry)  # isinstance check only
    player = SimpleNamespace(current_entry=entry)
    with pytest.raises(exceptions.CommandError):
        asyncio.run(
            bot_cls.cmd_speed(SimpleNamespace(), ssd_=None, guild=None, player=player, new_speed=value)
        )


def test_cache_lookup_stays_inside_the_cache_folder(tmp_path):
    from musicbot.filecache import AudioFileCache

    cache = tmp_path / "audio_cache"
    cache.mkdir()
    (tmp_path / "youtube-abc.webm").write_bytes(b"decoy next to the cache")
    (cache / "youtube-abc.webm").write_bytes(b"the cached file")
    fake = SimpleNamespace(cache_path=cache)

    # Before: with_name() built tmp_path/youtube-abc.webm, outside the cache.
    found = AudioFileCache.get_if_cached(fake, "/elsewhere/youtube-abc.webm")
    assert pathlib.Path(found) == cache / "youtube-abc.webm"
    exact = AudioFileCache.get_if_cached(fake, "/elsewhere/youtube-abc.webm", ignore_ext=False)
    assert pathlib.Path(exact) == cache / "youtube-abc.webm"


def test_aliases_file_follows_the_write_dir(tmp_path):
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]); import musicbot; "
        "musicbot.set_write_base(sys.argv[2]); "
        "from musicbot.aliases import AliasesDefault; print(AliasesDefault.aliases_file)"
    )
    out = subprocess.run(
        [sys.executable, "-I", "-c", code, str(REPO_ROOT), str(tmp_path)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    # Before: <install>/config/aliases.json, whatever --write-dir said.
    assert pathlib.Path(out) == tmp_path.resolve() / "config" / "aliases.json"


# --- B12: leaveserver -----------------------------------------------------------


def _leave_bot(names):
    left = []

    def make_guild(i, name):
        async def leave():
            left.append(name)

        return SimpleNamespace(id=i, name=name, owner=None, leave=leave)

    return SimpleNamespace(
        config=SimpleNamespace(owner_id=1),
        guilds=[make_guild(i, n) for i, n in enumerate(names, start=100)],
        get_guild=lambda _id: None,
        left=left,
    )


def test_leaveserver_accepts_a_one_word_name(bot_cls):
    fake = _leave_bot(["Test", "Other server"])
    message = SimpleNamespace(author=SimpleNamespace(id=1))  # for @owner_only
    asyncio.run(bot_cls.cmd_leaveserver(fake, ssd_=None, val="Test", leftover_args=[]))
    # Before: "You must provide an ID or name."
    assert fake.left == ["Test"]
    assert message


def test_leaveserver_unknown_name_is_a_clean_error(bot_cls):
    fake = _leave_bot(["Test"])
    message = SimpleNamespace(author=SimpleNamespace(id=1))  # for @owner_only
    # Before: UnboundLocalError (leave_guild never assigned).
    with pytest.raises(exceptions.CommandError):
        asyncio.run(
            bot_cls.cmd_leaveserver(fake, ssd_=None, val="No", leftover_args=["such", "server"])
        )
    assert fake.left == [] and message


# --- Video links that also name a playlist (the &list= prompt) -----------------

ALBUM = "OLAK5uy_TestAlbumNotARealListId000000000"


@pytest.mark.parametrize(
    "url, expected",
    [
        # albums were missed: the old regex only knew list=PL...
        (f"https://www.youtube.com/watch?v=abcdefghijk&list={ALBUM}", ("abcdefghijk", ALBUM)),
        (f"https://music.youtube.com/watch?v=abcdefghijk&list={ALBUM}", ("abcdefghijk", ALBUM)),
        # parameter order and extra parameters no longer matter
        ("https://www.youtube.com/watch?list=PLabc123&v=abcdefghijk", ("abcdefghijk", "PLabc123")),
        ("https://www.youtube.com/watch?v=abcdefghijk&pp=xyz&list=PLabc123", ("abcdefghijk", "PLabc123")),
        # already worked
        ("https://www.youtube.com/watch?v=abcdefghijk&list=PLabc123", ("abcdefghijk", "PLabc123")),
        ("https://youtu.be/abcdefghijk?list=PLabc123", ("abcdefghijk", "PLabc123")),
        # no prompt: mixes, plain videos, other sites
        ("https://www.youtube.com/watch?v=abcdefghijk&list=RDabcdefghijk", None),
        ("https://www.youtube.com/watch?v=abcdefghijk", None),
        ("https://notyoutube.com/watch?v=abcdefghijk&list=PLabc123", None),
    ],
)
def test_playlist_prompt_for_video_links(bot_cls, url, expected):
    prompts = []

    def create_task(coro):
        args = coro.cr_frame.f_locals
        prompts.append((args["ignore_vid"], args["next_url"]))
        coro.close()

    fake = SimpleNamespace(create_task=create_task)
    asyncio.run(
        bot_cls._cmd_play_compound_link(
            fake, None, None, None, None, None, None, None, [], url, False
        )
    )
    if expected is None:
        assert prompts == []
    else:
        video_id, list_id = expected
        assert prompts == [(video_id, f"https://www.youtube.com/playlist?list={list_id}")]
