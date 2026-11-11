"""
Batch 7: yt-dlp and cookie hygiene (SEC2, B7, B9, C4, B12 UA, B14, SEC3).

Nothing here touches the network: yt-dlp's cookie jar is read from local
files, and extract_info is replaced where a search result is needed.
"""

import asyncio
import gc
import logging
import os
import pathlib
import threading
from types import SimpleNamespace

import discord
import pytest
import yt_dlp

from musicbot import exceptions, startup_checks
from musicbot.downloader import Downloader


def _write_cookies(path: pathlib.Path, value: str) -> None:
    path.write_bytes(
        (
            "# Netscape HTTP Cookie File\n"
            f".example.com\tTRUE\t/\tFALSE\t4102444800\ttestcookie\t{value}\n"
        ).encode()
    )


def _fake_bot(tmp_path: pathlib.Path, **overrides):
    config = dict(
        audio_cache_path=tmp_path / "audio_cache",
        downloader_threads_max=2,
        ytdlp_user_agent="",
        ytdlp_deno_path="",
        ytdlp_concurrent_frags=1,
        ytdlp_source_address="*",
        ytdlp_proxy="",
        debug_mode=False,
        debug_level=logging.INFO,
        cookies_path=tmp_path / "cookies.txt",
        default_search_service="ytsearch",
        enable_local_media=False,
        spotify_enabled=False,
    )
    config.update(overrides)
    return SimpleNamespace(config=SimpleNamespace(**config), loop=None, spotify=None)


@pytest.fixture
def make_downloader(tmp_path):
    made = []

    def make(**overrides):
        d = Downloader(_fake_bot(tmp_path, **overrides))
        made.append(d)
        return d

    yield make
    for d in made:
        d.thread_pool.shutdown(wait=True)


def _cookies_sent(d: Downloader) -> list:
    """Cookie values a download thread's yt-dlp instance holds."""

    def read():
        return sorted(c.value for c in d.unsafe_ytdl.cookiejar)

    return d.thread_pool.submit(read).result(10)


# --- B7: cookie changes take effect without a restart -----------------------


def test_new_cookies_file_takes_effect_without_restart(tmp_path, make_downloader):
    _write_cookies(tmp_path / "cookies.txt", "old")
    d = make_downloader()
    assert _cookies_sent(d) == ["old"]

    _write_cookies(tmp_path / "cookies.txt", "new")
    d.enable_ytdl_cookies()
    # Before: yt-dlp had cached the jar on first use, so "old" was still sent.
    assert _cookies_sent(d) == ["new"]


def test_cookies_off_stops_sending_them(tmp_path, make_downloader):
    _write_cookies(tmp_path / "cookies.txt", "secret")
    d = make_downloader()
    assert _cookies_sent(d) == ["secret"]

    d.disable_ytdl_cookies()
    assert not d.cookies_enabled
    assert _cookies_sent(d) == []


def test_cookies_on_loads_a_file_missing_at_startup(tmp_path, make_downloader):
    d = make_downloader()
    assert _cookies_sent(d) == []

    _write_cookies(tmp_path / "cookies.txt", "later")
    d.enable_ytdl_cookies()
    assert d.cookies_enabled
    assert _cookies_sent(d) == ["later"]


def test_reload_never_writes_old_cookies_back(tmp_path, make_downloader):
    """Guard for the design: old instances are dropped, not close()d,
    because close() saves the in-memory jar over the cookie file."""
    cookies = tmp_path / "cookies.txt"
    _write_cookies(cookies, "old")
    d = make_downloader()
    assert _cookies_sent(d) == ["old"]

    _write_cookies(cookies, "new")
    d.enable_ytdl_cookies()
    assert _cookies_sent(d) == ["new"]
    gc.collect()
    assert "\tnew\n" in cookies.read_text()


# --- B9: one YoutubeDL instance per thread ----------------------------------


def test_each_thread_gets_its_own_ytdl_instance(make_downloader):
    d = make_downloader()
    seen = {}

    def grab(name):
        seen[name] = (d.unsafe_ytdl, d.unsafe_ytdl, d.safe_ytdl)

    threads = [threading.Thread(target=grab, args=(n,)) for n in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)

    a_unsafe, a_again, a_safe = seen["a"]
    b_unsafe, _, _ = seen["b"]
    assert a_unsafe is a_again  # reused within a thread
    assert a_unsafe is not b_unsafe  # not shared between threads
    assert a_safe is not a_unsafe
    assert a_safe.params["ignoreerrors"] is True
    assert a_unsafe.params["ignoreerrors"] is False


# --- SEC2 / C4: options ------------------------------------------------------


def test_tls_checks_stay_on_and_netrc_is_not_read(make_downloader):
    params = make_downloader().unsafe_ytdl.params
    assert not params.get("nocheckcertificate")
    assert not params.get("usenetrc")


def test_deno_path_option_reaches_ytdlp(make_downloader):
    d = make_downloader(ytdlp_deno_path="~/denobin")
    runtimes = d.unsafe_ytdl.params["js_runtimes"]
    assert runtimes["deno"] == {"path": os.path.expanduser("~/denobin")}
    assert "node" in runtimes  # still the fallback


def test_no_deno_path_keeps_the_path_lookup(make_downloader):
    runtimes = make_downloader().unsafe_ytdl.params["js_runtimes"]
    assert runtimes == {"deno": {}, "node": {}}


def test_missing_deno_path_is_an_error(tmp_path, caplog):
    startup_checks.check_deno_path(str(tmp_path / "nowhere" / "deno"))
    assert [r for r in caplog.records if r.levelno == logging.ERROR]


def test_deno_path_to_a_folder_with_deno_is_fine(tmp_path, caplog):
    name = "deno.exe" if os.name == "nt" else "deno"
    (tmp_path / name).write_bytes(b"")
    startup_checks.check_deno_path(str(tmp_path))
    startup_checks.check_deno_path("")
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


# --- B14 and the B12 UA item: search results ---------------------------------

CHANNEL = {
    "_type": "url",
    "ie_key": "YoutubeTab",
    "id": "UCartist",
    "url": "https://www.youtube.com/channel/UCartist",
    "title": "Some Artist",
}
VIDEOS = [
    {
        "_type": "url",
        "ie_key": "Youtube",
        "id": vid,
        "url": f"https://www.youtube.com/watch?v={vid}",
        "title": f"Some Artist - Song {vid}",
    }
    for vid in ("aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc", "ddddddddddd")
]


@pytest.fixture
def fake_search(monkeypatch):
    """YouTube's results page with the artist's channel on top."""
    asked = []

    def extract_info(self, subject, *args, **kwargs):
        asked.append(subject)
        prefix, query = subject.split(":", 1)
        count = int(prefix[len("ytsearch") :] or 1)
        entries = [CHANNEL, *VIDEOS][:count]
        return {
            "_type": "playlist",
            "id": query,
            "title": query,
            "extractor": "youtube:search",
            "extractor_key": "YoutubeSearch",
            "entries": entries,
            "playlist_count": len(entries),
        }

    monkeypatch.setattr(yt_dlp.YoutubeDL, "extract_info", extract_info)
    return asked


def _extract(d: Downloader, subject: str):
    async def no_headers(*_args, **_kwargs):
        return {}

    d.get_url_headers = no_headers

    async def run():
        d.bot.loop = asyncio.get_running_loop()
        return await d.extract_info(subject, download=False, process=True)

    return asyncio.run(run())


def test_search_skips_a_channel_top_result(make_downloader, fake_search):
    info = _extract(make_downloader(), "ytsearch:Some Artist")
    # Before: the channel was queued as one "track" and failed when played.
    assert info.url == VIDEOS[0]["url"]
    assert info.title == VIDEOS[0]["title"]


def test_explicit_multi_result_search_is_untouched(make_downloader, fake_search):
    """cmd_search asks for N results and lists them all, channel included."""
    info = _extract(make_downloader(), "ytsearch3:Some Artist")
    assert fake_search == ["ytsearch3:Some Artist"]
    assert [e["id"] for e in info.data["entries"]] == ["UCartist", *[v["id"] for v in VIDEOS[:2]]]


def test_configured_user_agent_survives_an_extraction(make_downloader, fake_search):
    d = make_downloader(ytdlp_user_agent="TestAgent/1.0")
    _extract(d, "ytsearch:Some Artist")
    # Before: the inverted check replaced a configured UA after each extraction.
    assert d.http_req_headers["User-Agent"] == "TestAgent/1.0"
    assert d.unsafe_ytdl.params["http_headers"]["User-Agent"] == "TestAgent/1.0"


# --- SEC3 / B7: the setcookies command ---------------------------------------


@pytest.fixture
def i18n():
    from musicbot.i18n import I18n

    I18n()


class FakeDownloader:
    @staticmethod
    def count_cookies_in_file(path):
        return Downloader.count_cookies_in_file(path)

    def __init__(self, enabled: bool):
        self.cookies_enabled = enabled
        self.loads = 0

    def enable_ytdl_cookies(self):
        self.cookies_enabled = True
        self.loads += 1

    def disable_ytdl_cookies(self):
        self.cookies_enabled = False


class FakeAttachment:
    def __init__(self, data: bytes):
        self.data = data

    async def save(self, fp, **_kwargs):
        if hasattr(fp, "write"):
            fp.write(self.data)
        else:
            pathlib.Path(fp).write_bytes(self.data)
        return len(self.data)


class FakeDM(discord.abc.PrivateChannel):
    pass


def _cmd_bot(tmp_path, enabled: bool):
    from musicbot.bot import MusicBot

    deleted = []

    async def safe_delete_message(msg):
        deleted.append(msg)

    fake = SimpleNamespace(
        config=SimpleNamespace(
            cookies_path=tmp_path / "cookies.txt",
            disabled_cookies_path=tmp_path / "_cookies.txt",
            owner_id=1,
        ),
        downloader=FakeDownloader(enabled),
        safe_delete_message=safe_delete_message,
        deleted=deleted,
    )
    fake._check_cookies_file = lambda path: MusicBot._check_cookies_file(fake, path)
    return fake


def _setcookies(fake, message, opt=""):
    from musicbot.bot import MusicBot

    return asyncio.run(MusicBot.cmd_setcookies(fake, ssd_=None, message=message, opt=opt))


def _upload(channel, data: bytes):
    return SimpleNamespace(
        author=SimpleNamespace(id=1), channel=channel, attachments=[FakeAttachment(data)]
    )


def _new_cookies_bytes():
    return (
        b"# Netscape HTTP Cookie File\n"
        b".example.com\tTRUE\t/\tFALSE\t4102444800\ttestcookie\tnew\n"
    )


def test_setcookies_is_allowed_in_dms():
    from musicbot.bot import MusicBot

    assert MusicBot.cmd_setcookies.cmd_in_dm is True


def test_cookie_upload_in_a_server_channel_is_refused(tmp_path, i18n):
    fake = _cmd_bot(tmp_path, enabled=False)
    message = _upload(SimpleNamespace(guild=object()), _new_cookies_bytes())
    with pytest.raises(exceptions.CommandError):
        _setcookies(fake, message)
    assert not (tmp_path / "cookies.txt").exists()
    assert fake.deleted == [message]
    assert fake.downloader.loads == 0


def test_cookie_upload_in_dm_replaces_and_reloads(tmp_path, i18n):
    _write_cookies(tmp_path / "cookies.txt", "old")
    fake = _cmd_bot(tmp_path, enabled=True)
    message = _upload(FakeDM(), _new_cookies_bytes())
    _setcookies(fake, message)
    assert "\tnew\n" in (tmp_path / "cookies.txt").read_text()
    # Before: cookies were already on, so nothing reloaded them.
    assert fake.downloader.loads == 1
    assert sorted(p.name for p in tmp_path.iterdir()) == ["cookies.txt"]
    if os.name == "posix":
        assert (tmp_path / "cookies.txt").stat().st_mode & 0o077 == 0


@pytest.mark.parametrize(
    "data",
    [b"hello, not cookies\n", b'[{"name": "x", "value": "y"}]\n', b"# Netscape HTTP Cookie File\n"],
    ids=["text", "json", "no-cookies"],
)
def test_bad_cookie_upload_keeps_the_old_cookies(tmp_path, i18n, data):
    _write_cookies(tmp_path / "cookies.txt", "old")
    fake = _cmd_bot(tmp_path, enabled=True)
    message = _upload(FakeDM(), data)
    with pytest.raises(exceptions.CommandError):
        _setcookies(fake, message)
    assert "\told\n" in (tmp_path / "cookies.txt").read_text()
    assert fake.downloader.loads == 0
    assert sorted(p.name for p in tmp_path.iterdir()) == ["cookies.txt"]


def test_setcookies_on_reloads_when_already_on(tmp_path, i18n):
    _write_cookies(tmp_path / "cookies.txt", "copied-over")
    fake = _cmd_bot(tmp_path, enabled=True)
    message = SimpleNamespace(author=SimpleNamespace(id=1), channel=FakeDM(), attachments=[])
    # Before: "Cookies already enabled." and the new file was ignored.
    _setcookies(fake, message, opt="on")
    assert fake.downloader.loads == 1
