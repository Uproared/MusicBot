import logging
import re
import sys
from importlib.metadata import PackageNotFoundError, version

import discord
from discord import opus

from .exceptions import HelpfulError

log = logging.getLogger(__name__)


def check_davey_version() -> None:
    """
    Refuse to start with a davey release known to break voice on this discord.py.

    discord.py 2.7.0/2.7.1 only checks that davey imports. With davey 0.2 the
    bot joins voice and shows as speaking, but plays silence (discord.py issue
    #10549). Keep this in step with the davey pin in requirements.txt.
    A missing davey is left to discord.py, which raises its own error.
    """
    try:
        davey_ver = version("davey")
    except PackageNotFoundError:
        return

    m = re.match(r"(\d+)\.(\d+)", davey_ver)
    if not m:
        log.warning("Could not parse davey version %r, skipping its check.", davey_ver)
        return

    dpy_ver = tuple(discord.version_info[:3])
    if (2, 7, 0) <= dpy_ver <= (2, 7, 1) and (int(m[1]), int(m[2])) >= (0, 2):
        raise HelpfulError(
            # fmt: off
            "Cannot start MusicBot: davey %(davey)s is installed, but discord.py %(dpy)s\n"
            "needs davey below 0.2. With this combination voice connects but plays no audio.\n"
            "Fix:  %(py_bin)s -m pip install \"davey>=0.1.6,<0.2\"",
            # fmt: on
            fmt_args={
                "davey": davey_ver,
                "dpy": discord.__version__,
                "py_bin": sys.executable,
            },
        )


def load_opus_lib() -> None:
    """
    Take steps needed to load opus library through discord.py
    """
    if opus.is_loaded():
        return

    try:
        opus._load_default()  # pylint: disable=protected-access
        return
    except OSError:
        pass

    raise RuntimeError("Could not load an opus lib.")
