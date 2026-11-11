"""
Batch 5: persistent queue robustness (B6).
"""

import asyncio
import json
from collections import defaultdict
from types import SimpleNamespace

import pytest

from musicbot.player import MusicPlayer
from musicbot.playlist import Playlist


def make_serializing_bot(tmp_path, player):
    """Enough of MusicBot for serialize_queue()."""
    guild_dir = tmp_path / "1"
    guild_dir.mkdir()
    return SimpleNamespace(
        config=SimpleNamespace(persistent_queue=True, data_path=tmp_path),
        get_player_in=lambda guild: player,
        aiolocks=defaultdict(asyncio.Lock),
    )


def test_queue_is_written_by_swapping_in_a_complete_file(tmp_path):
    from musicbot.bot import MusicBot  # heavy import, only for these tests

    player = SimpleNamespace(serialize=lambda sort_keys: '{"new": true}')
    fake_bot = make_serializing_bot(tmp_path, player)
    asyncio.run(MusicBot.serialize_queue(fake_bot, SimpleNamespace(id=1)))

    assert json.loads((tmp_path / "1" / "queue.json").read_text()) == {"new": True}
    assert not list((tmp_path / "1").glob("*.tmp"))


def test_failed_save_leaves_the_previous_queue_intact(tmp_path):
    """The old code opened queue.json with "w" first, so any failure left it empty."""
    from musicbot.bot import MusicBot

    def broken_serialize(sort_keys):
        raise RuntimeError("killed mid-write")

    fake_bot = make_serializing_bot(tmp_path, SimpleNamespace(serialize=broken_serialize))
    queue_file = tmp_path / "1" / "queue.json"
    queue_file.write_text('{"old": true}')

    with pytest.raises(RuntimeError):
        asyncio.run(MusicBot.serialize_queue(fake_bot, SimpleNamespace(id=1)))
    assert queue_file.read_text() == '{"old": true}'


def test_stale_queue_file_gives_an_empty_queue_instead_of_an_error(current_loop):
    """Valid JSON in an old or unexpected shape raised KeyError out of get_player."""
    stale = json.dumps({"__class__": "MusicPlayer", "__module__": "musicbot.player", "data": {}})
    fake = SimpleNamespace()
    assert MusicPlayer.from_json(stale, fake, fake, fake) is None


def test_corrupt_queue_file_gives_an_empty_queue(current_loop):
    assert MusicPlayer.from_json('{"truncated": ', None, None, None) is None


def test_entries_that_could_not_be_restored_are_skipped(current_loop):
    """Entry._deserialize returns None for a version mismatch or bad data."""
    fake_bot = SimpleNamespace(loop=current_loop)
    good = SimpleNamespace(title="ok")
    pl = Playlist._deserialize({"entries": [None, good, None]}, bot=fake_bot)
    assert list(pl.entries) == [good]


def test_saved_queue_round_trip(current_loop):
    """An empty saved queue loads back as an empty, working player."""
    from musicbot.player import MusicPlayer as Player

    bot = SimpleNamespace(loop=current_loop, config=SimpleNamespace(default_volume=0.15))
    playlist = Playlist(bot)
    voice = SimpleNamespace()
    saved = Player(bot, voice, playlist).serialize(sort_keys=True)

    restored = Player.from_json(saved, bot, voice, Playlist(bot))
    assert isinstance(restored, Player)
    assert not restored.playlist.entries and restored.current_entry is None
