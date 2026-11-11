"""
How MusicBot looks in Discord (the polish batch): the progress bar, the short
version shown in the embed footer, the player buttons, the header line on
command replies and the help categories.

The cards themselves are built in bot.py, next to the code that sends them.
"""

import re
from typing import TYPE_CHECKING, Dict, Iterable, List, Optional, Tuple

import discord

if TYPE_CHECKING:
    from .bot import MusicBot
    from .player import MusicPlayer

# Fixed custom_ids, so buttons on a card sent before a restart still reach the
# bot: one PlayerControls is registered at startup as a persistent view.
BUTTON_PAUSE_RESUME = "musicbot:player:pause_resume"
BUTTON_SKIP = "musicbot:player:skip"
BUTTON_REPEAT = "musicbot:player:repeat"
BUTTON_SHUFFLE = "musicbot:player:shuffle"
BUTTON_QUEUE = "musicbot:player:queue"

EMOJI_PLAY = "▶️"
EMOJI_PAUSE = "⏸️"
EMOJI_SKIP = "⏭️"
EMOJI_REPEAT = "\U0001f501"
EMOJI_REPEAT_ONE = "\U0001f502"
EMOJI_SHUFFLE = "\U0001f500"
EMOJI_QUEUE = "\U0001f4dc"
EMOJI_NOTES = "\U0001f3b6"
EMOJI_LIVE = "\U0001f534"

# Seconds between progress-bar updates on the newest now-playing card.
NP_PROGRESS_INTERVAL = 15

EMOJI_WARNING = "⚠️"
EMOJI_HELP = "❓"

# The help list's groups, in order. Commands not listed here go in "Other".
HELP_CATEGORIES: List[Tuple[str, Tuple[str, ...]]] = [
    ("\U0001f3b5 Play", ("play", "playnext", "playnow", "shuffleplay", "search", "stream")),
    (
        "⏯️ Player",
        ("np", "pause", "resume", "skip", "seek", "speed", "volume", "repeat"),
    ),
    ("\U0001f4dc Queue", ("queue", "shuffle", "move", "remove", "clear", "karaoke")),
    ("\U0001f50a Voice", ("summon", "disconnect", "follow")),
    ("\U0001f3b6 Autoplaylist", ("autoplaylist", "resetplaylist")),
    (
        "ℹ️ Info",
        (
            "help", "id", "perms", "listids", "latency", "botlatency", "uptime",
            "botversion", "checkupdates", "pldump",
        ),
    ),
    (
        "⚙️ Settings",
        (
            "config", "option", "setperms", "setprefix", "language", "setalias",
            "setcookies", "cache", "clean",
        ),
    ),
    (
        "\U0001f6e0️ Bot admin",
        (
            "restart", "shutdown", "setname", "setnick", "setavatar", "joinserver",
            "leaveserver", "blockuser", "blocksong",
        ),
    ),
]
HELP_OTHER = "\U0001f4e6 Other"

# Icons for the header line on command replies. Commands without one use
# their help group's icon.
COMMAND_ICONS: Dict[str, str] = {
    "play": "\U0001f3b5",
    "playnext": "\U0001f3b5",
    "playnow": "\U0001f3b5",
    "shuffleplay": "\U0001f500",
    "search": "\U0001f50e",
    "stream": "\U0001f4e1",
    "np": EMOJI_NOTES,
    "pause": EMOJI_PAUSE,
    "resume": EMOJI_PLAY,
    "skip": EMOJI_SKIP,
    "seek": "⏩",
    "speed": "⏩",
    "volume": "\U0001f50a",
    "repeat": EMOJI_REPEAT,
    "queue": EMOJI_QUEUE,
    "shuffle": EMOJI_SHUFFLE,
    "move": "↕️",
    "remove": "➖",
    "clear": "\U0001f9f9",
    "karaoke": "\U0001f3a4",
    "summon": "\U0001f4e5",
    "disconnect": "\U0001f4e4",
    "follow": "\U0001f463",
    "help": EMOJI_HELP,
    "restart": "\U0001f504",
    "shutdown": "\U0001f6d1",
}


def command_icon(name: str) -> str:
    """The icon for a command's reply header."""
    if name in COMMAND_ICONS:
        return COMMAND_ICONS[name]
    for label, names in HELP_CATEGORIES:
        if name in names:
            return label.split(" ", 1)[0]
    return "•"


def command_header(name: str, error: bool = False) -> str:
    """The header line on a command reply, e.g. '⏭️ skip', or '⚠️ skip' on errors."""
    return f"{EMOJI_WARNING if error else command_icon(name)} {name}"


def group_commands(names: Iterable[str]) -> List[Tuple[str, List[str]]]:
    """Sort command names into the help groups, in order, leaving out empty ones."""
    remaining = set(names)
    groups = []
    for label, members in HELP_CATEGORIES:
        found = [n for n in members if n in remaining]
        remaining.difference_update(found)
        if found:
            groups.append((label, found))
    if remaining:
        groups.append((HELP_OTHER, sorted(remaining)))
    return groups


# "<describe>-g<commit>-<branch>[-modded]", or "<commit>-<branch>..." when the
# repo has no tags (see constants.VERSION).
_VERSION_RE = re.compile(r"(?:^|-g)([0-9a-f]{7,40})-(.+?)(-modded)?$")


def short_version(version: str) -> str:
    """
    Shorten the version for the footer: 'release-250723-243-g102dd12-fixes'
    becomes 'fixes@102dd12'. A trailing '*' means local changes ('-modded').
    Anything else, like 'version_unknown', is returned unchanged.
    """
    m = _VERSION_RE.search(version)
    if not m:
        return version
    commit, branch, modded = m.groups()
    return f"{branch}@{commit}{'*' if modded else ''}"


def progress_bar(progress: float, total: float, width: int = 14) -> str:
    """A bar like ▬▬▬▬🔘▬▬▬▬▬▬▬▬▬ with the knob at progress / total."""
    frac = 0.0
    if total > 0:
        frac = min(max(progress / total, 0.0), 1.0)
    knob = min(int(frac * width), width - 1)
    return "▬" * knob + "\U0001f518" + "▬" * (width - 1 - knob)


def link_text(text: str) -> str:
    """Make `text` safe inside a markdown link label: no markdown, no brackets."""
    text = discord.utils.escape_markdown(text)
    return text.replace("[", "(").replace("]", ")")


class _PlayerButton(discord.ui.Button["PlayerControls"]):
    """One player button. A press goes to MusicBot.handle_player_button."""

    def __init__(
        self, custom_id: str, emoji: str, style: discord.ButtonStyle
    ) -> None:
        super().__init__(style=style, emoji=emoji, custom_id=custom_id)

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.view is None or self.custom_id is None:
            return
        await self.view.bot.handle_player_button(interaction, self.custom_id)


class PlayerControls(discord.ui.View):
    """
    The buttons under a now-playing card: pause/resume, skip, repeat, shuffle
    and queue. timeout=None plus fixed custom_ids make the view persistent.

    Each card gets its own instance built from the player's state, so the
    buttons show whether it's paused and the repeat mode. The checks (voice
    channel, permissions.ini, blocklist) happen in handle_player_button, the
    same as for typed commands.
    """

    def __init__(
        self, bot: "MusicBot", player: Optional["MusicPlayer"] = None
    ) -> None:
        super().__init__(timeout=None)
        self.bot = bot
        paused = bool(player and player.is_paused)
        repeat_song = bool(player and player.repeatsong)
        repeat_queue = bool(player and player.loopqueue)
        grey = discord.ButtonStyle.secondary
        lit = discord.ButtonStyle.primary

        self.add_item(
            _PlayerButton(
                BUTTON_PAUSE_RESUME,
                EMOJI_PLAY if paused else EMOJI_PAUSE,
                discord.ButtonStyle.success if paused else grey,
            )
        )
        self.add_item(_PlayerButton(BUTTON_SKIP, EMOJI_SKIP, grey))
        self.add_item(
            _PlayerButton(
                BUTTON_REPEAT,
                EMOJI_REPEAT_ONE if repeat_song else EMOJI_REPEAT,
                lit if (repeat_song or repeat_queue) else grey,
            )
        )
        self.add_item(_PlayerButton(BUTTON_SHUFFLE, EMOJI_SHUFFLE, grey))
        self.add_item(_PlayerButton(BUTTON_QUEUE, EMOJI_QUEUE, grey))
