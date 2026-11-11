"""
Fixes from the pre-deploy review of the fixes branch (2026-11-11).
"""

import asyncio
import os
from types import SimpleNamespace

import discord
import pytest

from musicbot import exceptions


@pytest.fixture
def bot_cls():
    from musicbot.bot import MusicBot
    from musicbot.i18n import I18n

    I18n()
    return MusicBot


async def _noop(*_args, **_kwargs):
    return None


# --- #1: a failure to start playback keeps autoplaylist tracks in the file ---


@pytest.mark.parametrize("starting, expect_file_update", [(True, False), (False, True)])
def test_start_failure_does_not_edit_autoplaylist_file(bot_cls, starting, expect_file_update):
    calls = []

    class AutoPlaylist:
        async def remove_track(self, _subject, *, ex=None, delete_from_ap=False):
            calls.append(delete_from_ap)

    fake = SimpleNamespace(
        config=SimpleNamespace(remove_ap=True),
        server_data={1: SimpleNamespace(autoplaylist=AutoPlaylist())},
    )
    player = SimpleNamespace(voice_client=SimpleNamespace(guild=SimpleNamespace(id=1)), is_dead=True)
    entry = SimpleNamespace(
        channel=None, from_auto_playlist=True, info=SimpleNamespace(input_subject="u")
    )
    error = discord.ClientException("ffmpeg was not found.")
    asyncio.run(
        bot_cls.on_player_error(fake, player=player, entry=entry, ex=error, starting=starting)
    )
    # Before: every start failure deleted the track from the file, so a
    # missing ffmpeg emptied the autoplaylist one track at a time.
    assert calls == [expect_file_update]


# --- #2: cache cleanup never deletes a local media file ----------------------


def test_cleanup_never_deletes_a_local_media_file(tmp_path, current_loop):
    from musicbot.entry import LocalFilePlaylistEntry
    from musicbot.player import MusicPlayer

    song = tmp_path / "song.mp3"
    song.write_bytes(b"the user's own file")
    entry = LocalFilePlaylistEntry.__new__(LocalFilePlaylistEntry)
    entry.filename = str(song)
    fake_player = SimpleNamespace(playlist=SimpleNamespace(entries=[]))
    current_loop.run_until_complete(MusicPlayer._handle_file_cleanup(fake_player, entry))
    # Before: with SaveVideos = no, this unlinked the file in the media folder.
    assert song.exists()


# --- #8: a failed queue save doesn't turn a working command into an error ---


class FakeGuildChannel(discord.abc.GuildChannel):
    def __init__(self, guild):
        self.guild = guild


def test_queue_save_failure_keeps_the_command_reply(bot_cls, caplog):
    from musicbot.constructs import Response

    sent = []

    async def safe_send_message(dest, content):
        sent.append(content)

    async def serialize_queue(_guild):
        raise PermissionError(13, "queue.json is locked")

    async def cmd_clear():
        return Response("Cleared the queue.")

    guild = SimpleNamespace(id=1)
    fake = SimpleNamespace(
        user=SimpleNamespace(id=42),
        config=SimpleNamespace(
            commands_via_mention=True,
            delete_invoking=False,
            bound_channels=set(),
            owner_id=7,
            bot_exception_ids=set(),
            user_blocklist=SimpleNamespace(is_blocked=lambda _u: False),
        ),
        wait_until_ready=_noop,
        server_data={1: SimpleNamespace(command_prefix="!", lang_code="", guild_id=1)},
        permissions=SimpleNamespace(
            for_user=lambda _u: SimpleNamespace(ignore_non_voice=[], name="Owner")
        ),
        cmd_clear=cmd_clear,
        serialize_queue=serialize_queue,
        safe_send_message=safe_send_message,
    )
    message = SimpleNamespace(
        channel=FakeGuildChannel(guild),
        guild=guild,
        content="!clear",
        author=SimpleNamespace(id=7, bot=False),
    )
    asyncio.run(bot_cls.on_message(fake, message))
    # Before: "Something went wrong running `clear`", though the queue was cleared.
    assert [r.description for r in sent] == ["Cleared the queue."]
    assert any("Could not save the queue" in r.getMessage() for r in caplog.records)


# --- #9: a failed cookie upload leaves no partial file -----------------------


class BrokenAttachment:
    async def save(self, fp, **_kwargs):
        fp.write(b"# Netscape HTTP Cookie File\n.example.com\tTRUE\t/\tF")
        raise OSError(28, "No space left on device")


class FakeDM(discord.abc.PrivateChannel):
    pass


def test_failed_cookie_upload_leaves_no_partial_file(bot_cls, tmp_path):
    fake = SimpleNamespace(
        config=SimpleNamespace(
            cookies_path=tmp_path / "cookies.txt",
            disabled_cookies_path=tmp_path / "_cookies.txt",
            owner_id=1,
        ),
        downloader=SimpleNamespace(cookies_enabled=False),
    )
    message = SimpleNamespace(
        author=SimpleNamespace(id=1), channel=FakeDM(), attachments=[BrokenAttachment()]
    )
    with pytest.raises(exceptions.CommandError):
        asyncio.run(bot_cls.cmd_setcookies(fake, ssd_=None, message=message, opt=""))
    # Before: cookies.txt.upload, with part of the cookies, stayed on disk.
    assert os.listdir(tmp_path) == []
