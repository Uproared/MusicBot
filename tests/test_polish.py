"""
Polish batch: the status line, the song cards, the queue list, colours and
footer, and the player buttons.

Each test drives the real code with small fakes; nothing connects to Discord.
"""

import asyncio
import collections
import datetime
from types import SimpleNamespace

import discord
import pytest

from musicbot import exceptions
from musicbot.constants import (
    DEFAULT_BOT_ICON,
    DEFAULT_BOT_NAME,
    DEFAULT_FOOTER_TEXT,
    MUSICBOT_EMBED_COLOR_PAUSED,
    MUSICBOT_EMBED_COLOR_PLAYING,
)
from musicbot.player_ui import (
    BUTTON_PAUSE_RESUME,
    BUTTON_QUEUE,
    BUTTON_REPEAT,
    BUTTON_SKIP,
    EMOJI_PAUSE,
    EMOJI_PLAY,
    EMOJI_REPEAT,
    EMOJI_REPEAT_ONE,
    PlayerControls,
    link_text,
    progress_bar,
    short_version,
)


@pytest.fixture
def bot_cls():
    from musicbot.bot import MusicBot
    from musicbot.i18n import I18n

    I18n()
    return MusicBot


async def _noop(*_args, **_kwargs):
    return None


def _entry(title="Song A", url="https://www.youtube.com/watch?v=abcdefghijk",
           seconds=204, author=None, uploader="Some Channel", thumb="https://i.example/t.jpg"):
    return SimpleNamespace(
        title=title,
        url=url,
        duration=seconds,
        duration_td=datetime.timedelta(seconds=seconds or 0),
        info={"uploader": uploader} if uploader else {},
        thumbnail_url=thumb,
        author=author,
        channel=object() if author else None,
    )


class _Playlist:
    def __init__(self, entries):
        self.entries = list(entries)

    def peek(self):
        return self.entries[0] if self.entries else None


def _player(current=None, queued=(), paused=False, repeatsong=False, loopqueue=False,
            progress=60.0):
    return SimpleNamespace(
        current_entry=current,
        playlist=_Playlist(queued),
        is_paused=paused,
        is_playing=not paused and current is not None,
        repeatsong=repeatsong,
        loopqueue=loopqueue,
        progress=progress,
    )


# --- small helpers ------------------------------------------------------------


@pytest.mark.parametrize(
    "version, expected",
    [
        ("release-250723-243-g102dd12-fixes", "fixes@102dd12"),
        ("release-250723-243-g102dd12-polish-modded", "polish@102dd12*"),
        ("102dd12-fixes", "fixes@102dd12"),
        ("release-250723-fixes", "release-250723-fixes"),  # exactly on a tag
        ("version_unknown", "version_unknown"),
    ],
)
def test_short_version(version, expected):
    assert short_version(version) == expected


@pytest.mark.parametrize(
    "progress, total, knob_at",
    [(0, 200, 0), (100, 200, 7), (200, 200, 13), (500, 200, 13), (10, 0, 0)],
)
def test_progress_bar_knob_position(progress, total, knob_at):
    bar = progress_bar(progress, total)
    assert len(bar) == 14
    assert bar.index("\U0001f518") == knob_at


def test_link_text_cannot_break_the_link():
    assert link_text("A [live] *mix*") == "A (live) \\*mix\\*"


# --- buttons: the view --------------------------------------------------------


def _buttons(view):
    return {item.custom_id: item for item in view.children}


def test_controls_are_persistent_and_show_the_state():
    async def run():
        idle = PlayerControls(bot=None)
        assert idle.is_persistent()
        b = _buttons(idle)
        assert str(b[BUTTON_PAUSE_RESUME].emoji) == EMOJI_PAUSE
        assert b[BUTTON_REPEAT].style == discord.ButtonStyle.secondary
        assert len(b) == 5

        paused = _buttons(PlayerControls(None, _player(_entry(), paused=True)))
        assert str(paused[BUTTON_PAUSE_RESUME].emoji) == EMOJI_PLAY
        assert paused[BUTTON_PAUSE_RESUME].style == discord.ButtonStyle.success

        song = _buttons(PlayerControls(None, _player(_entry(), repeatsong=True)))
        assert str(song[BUTTON_REPEAT].emoji) == EMOJI_REPEAT_ONE
        assert song[BUTTON_REPEAT].style == discord.ButtonStyle.primary

        queue = _buttons(PlayerControls(None, _player(_entry(), loopqueue=True)))
        assert str(queue[BUTTON_REPEAT].emoji) == EMOJI_REPEAT
        assert queue[BUTTON_REPEAT].style == discord.ButtonStyle.primary

    asyncio.run(run())


# --- status line --------------------------------------------------------------


def _status_bot(players, status_message=""):
    calls = []

    async def change_presence(status, activity):
        calls.append((status, activity))

    fake = SimpleNamespace(
        players=players,
        config=SimpleNamespace(
            status_include_paused=True, status_message=status_message, command_prefix="!"
        ),
        logout_called=False,
        last_status=None,
        aiolocks=None,
        change_presence=change_presence,
    )
    return fake, calls


def _run_status(bot_cls, fake, monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _noop)  # skips the 4 s rate-limit pause

    async def run():
        fake.aiolocks = collections.defaultdict(asyncio.Lock)
        await bot_cls.update_now_playing_status(fake)

    asyncio.run(run())


def test_status_listening_while_playing(bot_cls, monkeypatch):
    fake, calls = _status_bot({1: _player(_entry("Song A"))})
    _run_status(bot_cls, fake, monkeypatch)
    status, activity = calls[0]
    assert status == discord.Status.online
    assert activity.type == discord.ActivityType.listening
    assert activity.name == "Song A"


def test_status_paused(bot_cls, monkeypatch):
    fake, calls = _status_bot({1: _player(_entry("Song A"), paused=True)})
    _run_status(bot_cls, fake, monkeypatch)
    status, activity = calls[0]
    assert status == discord.Status.idle
    assert activity.state == f"{EMOJI_PAUSE} Paused: Song A"


def test_status_idle_says_how_to_start(bot_cls, monkeypatch):
    fake, calls = _status_bot({})
    _run_status(bot_cls, fake, monkeypatch)
    status, activity = calls[0]
    assert status == discord.Status.idle
    assert activity.state == "Nothing playing · !play"


def test_status_message_option_still_wins(bot_cls, monkeypatch):
    fake, calls = _status_bot({1: _player(_entry("Song A"))}, status_message="{p0_title}!")
    _run_status(bot_cls, fake, monkeypatch)
    assert calls[0][1].name == "Song A!"


# --- footer -------------------------------------------------------------------


def _footer_bot(footer_text=DEFAULT_FOOTER_TEXT, user=True):
    return SimpleNamespace(
        user=(
            SimpleNamespace(
                name="TestBot",
                display_avatar=SimpleNamespace(url="https://cdn.example/a.png"),
            )
            if user
            else None
        ),
        config=SimpleNamespace(footer_text=footer_text),
    )


def test_footer_shows_the_bots_name_avatar_and_short_version(bot_cls):
    from musicbot.constants import VERSION

    footer = bot_cls._footer(_footer_bot())
    assert footer == {
        "text": f"TestBot · {short_version(VERSION)}",
        "icon_url": "https://cdn.example/a.png",
    }


def test_custom_footer_text_still_applies(bot_cls):
    assert bot_cls._footer(_footer_bot("My footer"))["text"] == "My footer"


def test_footer_before_login_uses_the_defaults(bot_cls):
    footer = bot_cls._footer(_footer_bot(user=False))
    assert footer["text"].startswith(DEFAULT_BOT_NAME)
    assert footer["icon_url"] == DEFAULT_BOT_ICON


# --- now-playing card ---------------------------------------------------------


def _card_bot(embeds=True, mentions=False):
    return SimpleNamespace(config=SimpleNamespace(embeds=embeds, now_playing_mentions=mentions))


def _fields(card):
    return {f.name: f.value for f in card.fields}


def test_now_playing_card(bot_cls):
    author = SimpleNamespace(mention="<@7>", name="bob")
    cur = _entry("Song A", author=author)
    player = _player(cur, queued=[_entry("Song B"), _entry("Song C")])
    card = bot_cls._np_card(_card_bot(), player, cur, None)

    assert card.title == "Song A"
    assert card.url == cur.url
    assert "Now playing" in card.author.name
    assert card.colour == discord.Colour.from_str(MUSICBOT_EMBED_COLOR_PLAYING)
    assert card.description == "Some Channel"
    assert card.thumbnail.url == cur.thumbnail_url
    assert card.image.url is None  # small thumbnail, not the full-width image
    assert _fields(card) == {
        "Duration": "03:24",
        "Requested by": "<@7>",
        "Up next": "Song B (+1 more)",
    }


def test_np_card_with_progress_paused_and_repeating(bot_cls):
    cur = _entry("Song A")
    player = _player(cur, paused=True, repeatsong=True, progress=102)
    card = bot_cls._np_card(_card_bot(), player, cur, None, with_progress=True)

    assert "Paused" in card.author.name
    assert card.colour == discord.Colour.from_str(MUSICBOT_EMBED_COLOR_PAUSED)
    lines = card.description.split("\n")
    assert lines[1].startswith("`01:42` ") and lines[1].endswith(" `03:24`")
    assert "\U0001f518" in lines[1]
    assert lines[2].startswith(EMOJI_REPEAT_ONE)
    assert _fields(card)["Requested by"] == "Autoplaylist"
    assert _fields(card)["Up next"] == "Nothing queued"


def test_np_card_text_mode_doesnt_ping_and_local_files_have_no_link(bot_cls):
    author = SimpleNamespace(mention="<@7>", name="bob")
    cur = _entry("Local", url="/music/local.mp3", author=author, uploader=None)
    card = bot_cls._np_card(_card_bot(embeds=False), _player(cur), cur, None)
    assert _fields(card)["Requested by"] == "bob"
    assert card.url is None
    assert card.description is None or card.description == ""


def test_text_mode_keeps_the_card_heading():
    from musicbot.constructs import Response
    from musicbot.i18n import I18n

    I18n()
    card = Response("body", title="Song A")
    card.set_author(name="Now playing")
    assert card.to_markdown().startswith("**Now playing**\n## Song A\nbody")


# --- on_player_play sends the card with buttons --------------------------------


def test_now_playing_message_is_a_card_with_buttons(bot_cls):
    sent, retired, tracked = [], [], []

    async def safe_send_message(dest, content):
        sent.append((dest, content))
        return SimpleNamespace(id=99)

    async def retire(guild, edit=True):
        retired.append(guild)

    guild = SimpleNamespace(id=1)
    np_channel = object()
    ssd = SimpleNamespace(
        lang_code="", guild_id=1, last_np_msg=None, last_np_channel=np_channel,
        last_played_song_subject="", current_playing_url="",
    )
    author = SimpleNamespace(mention="<@7>", name="bob", id=7)
    cur = _entry("Song A", author=author)
    cur.from_auto_playlist = False
    fake = SimpleNamespace(
        server_data={1: ssd},
        config=SimpleNamespace(
            write_current_song=False, enable_queue_history_global=False,
            enable_queue_history_guilds=False, now_playing_mentions=False,
            dm_nowplaying=False, no_nowplaying_auto=False, nowplaying_channels=set(),
            bound_channels=set(), delete_nowplaying=False, embeds=True,
        ),
        permissions=SimpleNamespace(for_user=lambda _u: SimpleNamespace(skip_when_absent=False)),
        _handle_guild_auto_pause=_noop,
        reset_player_inactivity=_noop,
        update_now_playing_status=_noop,
        serialize_queue=_noop,
        safe_send_message=safe_send_message,
        _retire_np_controls=retire,
        _track_np_controls=lambda g, m, v: tracked.append((m.id, v)),
    )
    fake._np_card = lambda *a, **k: bot_cls._np_card(fake, *a, **k)
    player = _player(cur)
    player.voice_client = SimpleNamespace(
        channel=SimpleNamespace(guild=guild, members=[author], name="General"), guild=guild
    )
    player.skip_state = SimpleNamespace(reset=lambda: None)

    asyncio.run(bot_cls.on_player_play(fake, player=player, entry=cur))

    dest, card = sent[0]
    assert dest is np_channel
    assert card.title == "Song A"
    assert isinstance(card.view, PlayerControls)
    assert retired == [guild]  # the previous card lost its buttons first
    assert tracked == [(99, card.view)]


# --- the buttons' bookkeeping --------------------------------------------------


class _View:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


def test_retire_stops_the_view_and_removes_the_buttons(bot_cls):
    edits = []
    old_view = _View()
    msg = SimpleNamespace(edit=lambda **kw: _record(edits, kw))
    ssd = SimpleNamespace(np_controls_msg=msg, np_controls_view=old_view)
    fake = SimpleNamespace(server_data={1: ssd})

    asyncio.run(bot_cls._retire_np_controls(fake, SimpleNamespace(id=1)))
    assert old_view.stopped
    assert edits == [{"view": None}]
    assert ssd.np_controls_msg is None and ssd.np_controls_view is None


async def _record(store, item):
    store.append(item)


def test_refresh_stops_the_old_view_before_attaching_the_new_one(bot_cls):
    # Both views use the same custom_ids on the same message; stopping the old
    # one after the edit would unregister the new one's buttons.
    seen = []
    old_view = _View()

    async def edit(view):
        seen.append((old_view.stopped, view))

    ssd = SimpleNamespace(np_controls_msg=SimpleNamespace(edit=edit), np_controls_view=old_view)
    player = _player(_entry(), paused=True)
    fake = SimpleNamespace(server_data={1: ssd}, get_player_in=lambda g: player)

    asyncio.run(bot_cls.refresh_np_controls(fake, SimpleNamespace(id=1)))
    stopped_first, new_view = seen[0]
    assert stopped_first
    assert ssd.np_controls_view is new_view
    assert str(_buttons(new_view)[BUTTON_PAUSE_RESUME].emoji) == EMOJI_PLAY


# --- button presses -------------------------------------------------------------


class _Member:
    """Stands in for discord.Member (patched in) in the button tests."""

    def __init__(self, uid, voice_channel=None, name="pal"):
        self.id = uid
        self.display_name = name
        self.voice = SimpleNamespace(channel=voice_channel) if voice_channel else None


def _button_setup(monkeypatch, *, member_in_voice=True, can_use=True, instaskip=False,
                  blocked=False, owner=False, queued=1, stale=False):
    monkeypatch.setattr(discord, "Member", _Member)
    calls, refusals, announced, deferred, tasks = [], [], [], [], []
    vc = SimpleNamespace(mention="<#5>")
    member = _Member(1 if owner else 7, vc if member_in_voice else None)
    cur = _entry("Song A", author=SimpleNamespace(id=8))
    player = _player(cur, queued=[_entry("Song B")] * queued)
    player.voice_client = SimpleNamespace(channel=vc, is_connected=lambda: True)
    card = SimpleNamespace(id=50)
    card_edits = []

    async def card_edit(**kw):
        card_edits.append(kw)

    card.edit = card_edit
    ssd = SimpleNamespace(
        lang_code="", guild_id=1,
        np_controls_msg=SimpleNamespace(id=49 if stale else 50),
        np_controls_view=None,
    )

    def cmd(name, result=None):
        async def run(*args):
            calls.append((name, args))
            return result
        return run

    async def refusal(_interaction, text):
        refusals.append(text)

    async def announce(_interaction, text):
        announced.append(text)

    async def defer():
        deferred.append(True)

    from musicbot.constructs import Response

    perms = SimpleNamespace(
        name="Default", instaskip=instaskip, can_use_command=lambda c, s="": can_use
    )
    fake = SimpleNamespace(
        server_data={1: ssd},
        config=SimpleNamespace(
            owner_id=1, allow_author_skip=True,
            user_blocklist=SimpleNamespace(is_blocked=lambda m: blocked),
        ),
        permissions=SimpleNamespace(for_user=lambda m: perms),
        get_player_in=lambda g: player,
        _button_refusal=refusal,
        _button_announce=announce,
        cmd_pause=cmd("pause", Response("Paused music in `General`")),
        cmd_resume=cmd("resume", Response("Resumed")),
        cmd_skip=cmd("skip", Response("Force skipped `Song A`.")),
        cmd_repeat=cmd("repeat", Response("Song is now repeating.")),
        cmd_shuffle=cmd("shuffle", Response("Shuffled all songs in the queue.")),
        refresh_np_controls=_noop,
        serialize_queue=_noop,
        create_task=lambda coro, name=None: (tasks.append(name), coro.close()),
        _button_queue=lambda *a: _noop(),
    )
    interaction = SimpleNamespace(
        guild=SimpleNamespace(id=1),
        user=member,
        message=card,
        channel=object(),
        response=SimpleNamespace(defer=defer),
    )
    return SimpleNamespace(
        fake=fake, interaction=interaction, calls=calls, refusals=refusals,
        announced=announced, deferred=deferred, tasks=tasks, card_edits=card_edits,
        player=player,
    )


def _press(bot_cls, s, button):
    asyncio.run(bot_cls.handle_player_button(s.fake, s.interaction, button))


def test_button_needs_the_bots_voice_channel(bot_cls, monkeypatch):
    s = _button_setup(monkeypatch, member_in_voice=False)
    _press(bot_cls, s, BUTTON_PAUSE_RESUME)
    assert s.calls == []
    assert s.refusals == ["Join <#5> to use the player buttons."]


def test_owner_may_press_from_outside_voice(bot_cls, monkeypatch):
    s = _button_setup(monkeypatch, member_in_voice=False, owner=True)
    _press(bot_cls, s, BUTTON_PAUSE_RESUME)
    assert [c[0] for c in s.calls] == ["pause"]


def test_button_follows_permissions_ini(bot_cls, monkeypatch):
    s = _button_setup(monkeypatch, can_use=False)
    _press(bot_cls, s, BUTTON_SKIP)
    assert s.calls == []
    assert "not allowed for your permissions group: Default" in s.refusals[0]


def test_blocked_user_cant_press(bot_cls, monkeypatch):
    s = _button_setup(monkeypatch, blocked=True)
    _press(bot_cls, s, BUTTON_PAUSE_RESUME)
    assert s.calls == [] and s.refusals == ["You can't use this bot."]


def test_old_card_is_refused_and_loses_its_buttons(bot_cls, monkeypatch):
    s = _button_setup(monkeypatch, stale=True)
    _press(bot_cls, s, BUTTON_PAUSE_RESUME)
    assert s.calls == []
    assert "out of date" in s.refusals[0]
    assert s.card_edits == [{"view": None}]


def test_pause_button_announces_who_paused(bot_cls, monkeypatch):
    s = _button_setup(monkeypatch)
    _press(bot_cls, s, BUTTON_PAUSE_RESUME)
    assert [c[0] for c in s.calls] == ["pause"]
    assert s.deferred == [True]
    assert s.announced == [f"{EMOJI_PAUSE} **pal** paused the music."]


def test_pause_button_resumes_when_paused(bot_cls, monkeypatch):
    s = _button_setup(monkeypatch)
    s.player.is_paused = True
    _press(bot_cls, s, BUTTON_PAUSE_RESUME)
    assert [c[0] for c in s.calls] == ["resume"]


@pytest.mark.parametrize("instaskip, param", [(True, "f"), (False, "")])
def test_skip_button_forces_only_for_those_allowed(bot_cls, monkeypatch, instaskip, param):
    s = _button_setup(monkeypatch, instaskip=instaskip)
    _press(bot_cls, s, BUTTON_SKIP)
    name, args = s.calls[0]
    assert name == "skip" and args[-1] == param
    if instaskip:
        assert s.announced[0].endswith("**pal** skipped `Song A`.")
    else:  # a vote: the command's own reply, signed
        assert s.announced[0].endswith("**pal**: Force skipped `Song A`.")


def test_command_error_goes_only_to_the_presser(bot_cls, monkeypatch):
    s = _button_setup(monkeypatch)

    async def failing(*_args):
        raise exceptions.CommandError("Player is not playing.")

    s.fake.cmd_pause = failing
    _press(bot_cls, s, BUTTON_PAUSE_RESUME)
    assert s.refusals == ["Player is not playing."]
    assert s.announced == []


def test_queue_button(bot_cls, monkeypatch):
    s = _button_setup(monkeypatch, queued=0)
    _press(bot_cls, s, BUTTON_QUEUE)
    assert s.refusals == ["There are no songs queued."] and s.tasks == []

    s = _button_setup(monkeypatch, queued=2)
    _press(bot_cls, s, BUTTON_QUEUE)
    assert s.tasks == ["MB_QueueButton"] and s.deferred == [True]


# --- queue list -------------------------------------------------------------------


def _queue_text(bot_cls, embeds):
    sent = []

    async def safe_send_message(_dest, content):
        sent.append(content)
        return SimpleNamespace(id=1)

    bob = SimpleNamespace(name="bob")
    cur = _entry("Song A", author=bob)
    player = _player(cur, queued=[_entry("Song B", author=bob, seconds=180),
                                  _entry("Song [C]", seconds=60)])
    fake = SimpleNamespace(
        config=SimpleNamespace(embeds=embeds, queue_length=10, delete_delay_long=60),
        safe_send_message=safe_send_message,
    )
    asyncio.run(bot_cls.cmd_queue(fake, None, SimpleNamespace(id=1), object(), player))
    return sent[0]


def test_queue_list_is_compact_with_links_and_totals(bot_cls):
    q = _queue_text(bot_cls, embeds=True)
    lines = q.description.split("\n")
    assert lines[0] == f"{EMOJI_PLAY} **[Song A](https://www.youtube.com/watch?v=abcdefghijk)**"
    assert lines[1] == "`01:00 / 03:24` · bob"
    assert "`1.` [Song B](https://www.youtube.com/watch?v=abcdefghijk) · `03:00` · bob" in lines
    assert "`2.` [Song (C)](https://www.youtube.com/watch?v=abcdefghijk) · `01:00` · autoplaylist" in lines
    assert lines[-1] == "2 songs · 04:00 total"


def test_queue_list_text_mode_has_no_links(bot_cls):
    q = _queue_text(bot_cls, embeds=False)
    assert "](" not in q.description
    assert "`1.` Song B · `03:00` · bob" in q.description
