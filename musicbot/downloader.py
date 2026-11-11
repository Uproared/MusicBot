import asyncio
import copy
import datetime
import functools
import hashlib
import logging
import os
import pathlib
import threading
from collections import UserDict
from concurrent.futures import ThreadPoolExecutor
from pprint import pformat
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple, Union

import aiohttp
import yt_dlp as youtube_dl  # type: ignore[import-untyped]
from yt_dlp.cookies import YoutubeDLCookieJar  # type: ignore[import-untyped]
from yt_dlp.networking.exceptions import (  # type: ignore[import-untyped]
    NoSupportingHandlers,
)
from yt_dlp.utils import DownloadError  # type: ignore[import-untyped]
from yt_dlp.utils import UnsupportedError

from .constants import DEFAULT_MAX_INFO_REQUEST_TIMEOUT
from .exceptions import ExtractionError, MusicbotException
from .i18n import _L
from .spotify import Spotify
from .utils import check_extractor

if TYPE_CHECKING:
    from multidict import CIMultiDictProxy

    from .bot import MusicBot

    # Explicit compat with python 3.8
    YUserDict = UserDict[str, Any]
else:
    YUserDict = UserDict


log = logging.getLogger(__name__)


class YtdlpLogHook:
    def debug(self, msg: Any) -> None:
        """
        Debug or info level log from yt_dlp. With a logger attached, yt-dlp
        sends all its progress output here (extracting, downloading, %), so
        keep it at DEBUG now that the logger is always attached (S3).
        """
        ld = log.debug
        ld(f"[YTDLP] {msg}")  # pylint: disable=logging-fstring-interpolation

    def info(self, msg: Any) -> None:
        """Info level log from yt_dlp"""
        li = log.info
        li(f"[YTDLP] {msg}")  # pylint: disable=logging-fstring-interpolation

    def warning(self, msg: Any) -> None:
        """Warning level log from yt_dlp"""
        lw = log.warning
        lw(f"[YTDLP] {msg}")  # pylint: disable=logging-fstring-interpolation

    def error(self, msg: Any) -> None:
        """Error level log from yt_dlp"""
        le = log.error
        le(f"[YTDLP] {msg}")  # pylint: disable=logging-fstring-interpolation


# Immutable dict is needed, because something is modifying the 'outtmpl' value. I suspect it being ytdl, but I'm not sure.
ytdl_format_options_immutable = MappingProxyType(
    {
        "format": "bestaudio/best",
        "outtmpl": "%(extractor)s-%(id)s-%(title).64B-%(qhash)s.%(ext)s",
        "restrictfilenames": True,
        "noplaylist": True,
        # SEC2: "nocheckcertificate" (TLS checks off, cookies included) and
        # "usenetrc" (reads ~/.netrc) were removed. Neither is needed here.
        "ignoreerrors": False,
        "logtostderr": False,
        "quiet": True,
        "no_warnings": True,
        # extract_flat speeds up extract_info by only listing playlist entries rather than extracting them as well.
        "extract_flat": "in_playlist",
        "default_search": "auto",
        "source_address": None,
        # yt-dlp's current form of no_color. The old key made every YoutubeDL
        # instance after the first warn "Overwriting params from color".
        "color": "no_color",
        "retries": 1,
    }
)


def _ytdlp_bug_msg(*_args: Any, **_kwargs: Any) -> str:
    """
    Removes bug report text from exceptions to clean them up for musicbot.
    It also issues a debug message to let users/devs know that ytdlp thinks the
    error could be a bug worth reporting.
    """
    log.debug("YTDLP thinks there may be a bug in processing.")
    return ""


youtube_dl.utils.bug_reports_message = _ytdlp_bug_msg

"""
    Alright, here's the problem.  To catch youtube-dl errors for their useful information, I have to
    catch the exceptions with `ignoreerrors` off.  To not break when ytdl hits a dumb video
    (rental videos, etc), I have to have `ignoreerrors` on.  I can change these whenever, but with async
    that's bad.  So I need multiple ytdl objects.

"""


class Downloader:
    def __init__(self, bot: "MusicBot") -> None:
        """
        Set up YoutubeDL and related config as well as a thread pool executor
        to run concurrent extractions.
        """
        self.bot: MusicBot = bot
        self.download_folder: pathlib.Path = bot.config.audio_cache_path
        # NOTE: this executor may not be good for long-running downloads...
        self.thread_pool = ThreadPoolExecutor(
            max_workers=bot.config.downloader_threads_max,
            thread_name_prefix="MB_Downloader",
        )
        self._supported_search = [
            "ytsearch",  # YouTube search
            "gvsearch",  # Google Video search
            "yvsearch",  # Yahoo video search
            "scsearch",  # soundcloud search
            "bilisearch",  # bilibili search
            "nicosearch",  # nico video search
        ]

        # force ytdlp and HEAD requests to use the same UA string.
        # If the constant is set, use that, otherwise pick one random UA for
        # this process. It no longer changes per request: that code was inverted
        # (it replaced a configured UA), and a stable UA suits cookies (B12).
        if bot.config.ytdlp_user_agent:
            ua = bot.config.ytdlp_user_agent
            log.warning("Forcing YTDLP to use User Agent:  %s", ua)
        else:
            ua = youtube_dl.utils.networking.random_user_agent()
        self.http_req_headers = {"User-Agent": ua}
        # Copy immutable dict and use the mutable copy for everything else.
        ytdl_format_options = ytdl_format_options_immutable.copy()
        ytdl_format_options["http_headers"] = self.http_req_headers

        # Since yt-dlp version 2025.11.12, EJS and a js runtime are needed.
        # This should enable nodejs as a fallback to deno, if available.
        # Value is a dict of: {'runtime': {'path': '/opt/path/config'}, ...}
        ytdl_format_options["js_runtimes"] = {
            "deno": {},
            "node": {},
        }
        # C4: YtdlpDenoPath points yt-dlp at a deno binary (or its folder)
        # that isn't on PATH.
        if bot.config.ytdlp_deno_path:
            ytdl_format_options["js_runtimes"]["deno"] = {
                "path": os.path.expanduser(bot.config.ytdlp_deno_path)
            }

        # add concurrent-fragments option if it is needed.
        if bot.config.ytdlp_concurrent_frags > 1:
            ytdl_format_options["concurrent_fragment_downloads"] = (
                bot.config.ytdlp_concurrent_frags
            )

        # apply source address settings.
        if bot.config.ytdlp_source_address != "*":
            ytdl_format_options["source_address"] = bot.config.ytdlp_source_address

        # apply download concurrency settings.
        if bot.config.ytdlp_concurrent_frags > 1:
            ytdl_format_options["concurrent_fragment_downloads"] = (
                bot.config.ytdlp_concurrent_frags
            )

        # Always attach the logger, so yt-dlp warnings (no JavaScript runtime,
        # cookie problems, missing formats) reach the log at any DebugLevel.
        # They were dropped unless debug mode was on (S3).
        ytdl_format_options["no_warnings"] = False
        ytdl_format_options["logger"] = YtdlpLogHook()
        # enable verbose ytdlp logs if debug mode is enabled.
        if (
            bot.config.debug_mode
            and bot.config.debug_level <= logging.NOISY  # type: ignore[attr-defined]
        ):
            ytdl_format_options["verbose"] = True

        # check if we should apply a cookies file to ytdlp.
        use_cookies = bot.config.cookies_path.is_file()
        if use_cookies:
            log.info(
                "MusicBot will use cookies for yt-dlp from:  %s",
                bot.config.cookies_path,
            )

        if bot.config.ytdlp_proxy:
            log.info("Yt-dlp will use your configured proxy server.")
            ytdl_format_options["proxy"] = bot.config.ytdlp_proxy

        if self.download_folder:
            # print("setting template to " + os.path.join(download_folder, otmpl))
            otmpl = ytdl_format_options["outtmpl"]
            ytdl_format_options["outtmpl"] = os.path.join(
                self.download_folder, str(otmpl)
            )

        # YoutubeDL instances are made per download thread from these options
        # (B9: two threads shared one instance), and remade after a cookie
        # change (B7: yt-dlp reads the cookie file once, on first use, so
        # changing params["cookiefile"] afterwards did nothing).
        self._ytdl_options: Dict[str, Any] = ytdl_format_options
        self._ytdl_local = threading.local()
        # (generation, cookies on), replaced as one tuple so a thread never
        # sees a new generation with the old cookie setting.
        self._ytdl_state: Tuple[int, bool] = (0, use_cookies)
        # Only for prepare_filename() on the event loop; never makes requests.
        self._filename_ytdl = youtube_dl.YoutubeDL(self._ytdl_params(False))

    def _ytdl_params(self, cookies: bool, **overrides: Any) -> Dict[str, Any]:
        """A fresh options dict: YoutubeDL keeps and edits the one it's given."""
        params = {
            k: dict(v) if isinstance(v, dict) else v
            for k, v in self._ytdl_options.items()
        }
        if cookies:
            # str: on Windows with HOME set, yt-dlp calls .startswith on it.
            params["cookiefile"] = str(self.bot.config.cookies_path)
        params.update(overrides)
        return params

    def _thread_ytdl(self, safe: bool) -> youtube_dl.YoutubeDL:
        """
        The calling thread's YoutubeDL instance: the safe one ignores errors.
        Made on first use in each thread, and again after a cookie change.
        """
        local = self._ytdl_local
        state = self._ytdl_state
        if getattr(local, "state", None) != state:
            # Old instances are dropped, not closed: close() writes their
            # in-memory cookies back to the cookie file, which would undo a
            # cookies.txt that was just replaced. A call still running on an
            # old instance finishes on it.
            local.instances = {}
            local.state = state
        if safe not in local.instances:
            overrides = {"ignoreerrors": True} if safe else {}
            local.instances[safe] = youtube_dl.YoutubeDL(
                self._ytdl_params(state[1], **overrides)
            )
        return local.instances[safe]

    @property
    def unsafe_ytdl(self) -> youtube_dl.YoutubeDL:
        """This thread's YoutubeDL instance that raises errors."""
        return self._thread_ytdl(safe=False)

    @property
    def safe_ytdl(self) -> youtube_dl.YoutubeDL:
        """This thread's YoutubeDL instance with errors ignored."""
        return self._thread_ytdl(safe=True)

    def _extract_in_thread(self, safe: bool, *args: Any, **kwargs: Any) -> Any:
        """Runs in a download thread: extract_info on that thread's instance."""
        return self._thread_ytdl(safe).extract_info(*args, **kwargs)

    @property
    def cookies_enabled(self) -> bool:
        """
        Get status of the cookies file use in yt-dlp.
        """
        return self._ytdl_state[1]

    def _set_ytdl_cookies(self, enabled: bool) -> None:
        """New YoutubeDL instances on each thread's next call, with or without cookies."""
        generation = self._ytdl_state[0] + 1
        self._ytdl_state = (generation, enabled)

    def enable_ytdl_cookies(self) -> None:
        """
        Use the cookies file, (re)reading it, even if cookies were already on.
        """
        self._set_ytdl_cookies(True)
        log.info("yt-dlp cookies (re)loaded from:  %s", self.bot.config.cookies_path)

    def disable_ytdl_cookies(self) -> None:
        """
        Stop sending cookies.
        """
        self._set_ytdl_cookies(False)
        log.info("yt-dlp cookies disabled.")

    @staticmethod
    def count_cookies_in_file(path: pathlib.Path) -> int:
        """
        Read a cookies.txt with yt-dlp's own parser and count the cookies.
        Raises OSError (http.cookiejar.LoadError included) or ValueError if
        yt-dlp couldn't use it.
        """
        jar = YoutubeDLCookieJar(path)
        jar.load()
        return len(jar)

    def get_url_or_none(self, url: str) -> Optional[str]:
        """
        Uses ytdl.utils.url_or_none() to validate a playable URL.
        Will also strip < and > if they are found at the start and end of a URL.
        """
        # Discord might add < and > to the URL, this strips them out if they exist.
        if url.startswith("<") and url.endswith(">"):
            url = url[1:-1]
        u = youtube_dl.utils.url_or_none(url)

        # Just in case ytdlp changes... also strict typing.
        if isinstance(u, str):
            return u
        return None

    async def get_url_headers(self, url: str) -> Dict[str, str]:
        """
        Make an HTTP HEAD request and return response headers safe for serialization.
        Header names are converted to upper case.
        If `url` is not valid the header 'X-INVALID-URL' is set to its value.
        """
        test_url = self.get_url_or_none(url)
        headers: Dict[str, Any] = {}
        # do a HEAD request and add the headers to extraction info.
        if test_url and self.bot.session:
            try:
                head_data = await self._get_headers(
                    self.bot.session,
                    test_url,
                    timeout=DEFAULT_MAX_INFO_REQUEST_TIMEOUT,
                    req_headers=self.http_req_headers,
                )
                if not head_data:
                    raise ExtractionError("HEAD seems to have no headers...")

                # convert multidict headers to a serializable dict.
                for key in set(head_data.keys()):
                    new_key = key.upper()
                    values = head_data.getall(key)
                    if len(values) > 1:
                        headers[new_key] = values
                    else:
                        headers[new_key] = values.pop()
            except asyncio.exceptions.TimeoutError:
                log.warning("Checking media headers failed due to timeout.")
                headers = {"X-HEAD-REQ-FAILED": "1"}
            except (ExtractionError, OSError, aiohttp.ClientError):
                log.warning("Failed HEAD request for:  %s", test_url)
                log.exception("HEAD Request exception: ")
                headers = {"X-HEAD-REQ-FAILED": "1"}
        else:
            headers = {"X-INVALID-URL": url}
        return headers

    async def _get_headers(  # pylint: disable=dangerous-default-value
        self,
        session: aiohttp.ClientSession,
        url: str,
        *,
        timeout: int = 10,
        allow_redirects: bool = True,
        req_headers: Dict[str, Any] = {},
    ) -> Union["CIMultiDictProxy[str]", None]:
        """
        Uses given aiohttp `session` to fetch HEAD of given `url` without making
        any checks if the URL is valid.
        If `headerfield` is set, only the given header field is returned.

        :param: timeout:  Set a different timeout for the HEAD request.
        :param: allow_redirect:  Follow "Location" headers through, on by default.
        :param: req_headers:  Set a collection of headers to send with the HEAD request.

        :returns:  A case-insensitive multidict instance, not serializable.

        :raises:  aiohttp.ClientError and derived exceptions
            For errors handled internally by aiohttp.
        :raises:  OSError
            For errors not handled by aiohttp.
        """
        req_timeout = aiohttp.ClientTimeout(total=timeout)
        async with session.head(
            url,
            timeout=req_timeout,
            allow_redirects=allow_redirects,
            headers=req_headers,
            proxy=self.bot.config.ytdlp_proxy,
        ) as response:
            return response.headers

    def _sanitize_and_log(  # pylint: disable=dangerous-default-value
        self,
        data: Dict[str, Any],
        redact_fields: List[str] = [],
    ) -> None:
        """
        Debug helper function.
        Copies data, removes some long-winded entries and logs the result data for inspection.
        """
        if log.getEffectiveLevel() > logging.DEBUG:
            return

        data = copy.deepcopy(data)
        redacted_str = "__REDACTED_FOR_CLARITY__"

        if "entries" in data:
            # cleaning up entry data to make it easier to parse in logs.
            for i, e in enumerate(data["entries"]):
                for field in redact_fields:
                    if field in e and e[field]:
                        data["entries"][i][field] = redacted_str

        for field in redact_fields:
            if field in data:
                data[field] = redacted_str

        if log.getEffectiveLevel() <= logging.EVERYTHING:  # type: ignore[attr-defined]
            log.noise("Sanitized YTDL Extraction Info (not JSON):\n%s", pformat(data))  # type: ignore[attr-defined]
        else:
            log.noise("Sanitized YTDL Extraction Info (not JSON):  %s", data)  # type: ignore[attr-defined]

    async def extract_info(
        self, song_subject: str, *args: Any, **kwargs: Any
    ) -> "YtdlpResponseDict":
        """
        Runs ytdlp.extract_info with all arguments passed to this function.
        If `song_subject` is a valid URL, extraction will add HEAD request headers.
        Resulting data is passed through ytdlp's sanitize_info and returned
        inside of a YtdlpResponseDict wrapper.

        Single-entry search results are returned as if they were top-level extractions.
        Links for spotify tracks, albums, and playlists also get special filters.

        :param: song_subject: a song url or search subject.
        :kwparam: as_stream: If we should try to queue the URL anyway and let ffmpeg figure it out.

        :returns: YtdlpResponseDict object containing sanitized extraction data.

        :raises: musicbot.exceptions.MusicbotError
            if event loop is closed and cannot be used for extraction.

        :raises: musicbot.exceptions.ExtractionError
            for errors in MusicBot's internal filtering and pre-processing of extraction queries.

        :raises: musicbot.exceptions.SpotifyError
            for issues with Musicbot's Spotify API request and data handling.

        :raises: yt_dlp.utils.YoutubeDLError
            as a base exception for any exceptions raised by yt_dlp.

        :raises: yt_dlp.networking.exceptions.RequestError
            as a base exception for any networking errors raised by yt_dlp.
        """
        # handle local media playback without ever touching ytdl and friends.
        # We do this here so auto playlist features can take advantage of this as well.
        if self.bot.config.enable_local_media and song_subject.lower().startswith(
            "file://"
        ):
            return self._return_local_media(song_subject)

        if song_subject.lower().startswith("mbapl://"):
            log.debug("AutoPlaylist requested:  %s", song_subject)
            return await self._return_autoplaylist(song_subject)

        # Hash the URL for use as a unique ID in file paths.
        # but ignore services with multiple URLs for the same media.
        song_subject_hash = ""
        if (
            "youtube.com" not in song_subject.lower()
            and "youtu.be" not in song_subject.lower()
        ):
            md5 = hashlib.md5()  # nosec
            md5.update(song_subject.encode("utf8"))
            song_subject_hash = md5.hexdigest()[-8:]

        # Use ytdl or one of our custom integration to get info.
        data = await self._filtered_extract_info(
            song_subject,
            *args,
            **kwargs,
            # just (ab)use a ytdlp internal thing, a tiny bit...
            extra_info={
                "qhash": song_subject_hash,
            },
        )

        if not data:
            raise ExtractionError("Song info extraction returned no data.")

        # always get headers for our downloadable.
        headers = await self.get_url_headers(data.get("url", song_subject))

        # if we made it here, put our request data into the extraction.
        data["__input_subject"] = song_subject
        data["__header_data"] = headers or None
        data["__expected_filename"] = self._filename_ytdl.prepare_filename(data)

        """
        # disabled since it is only needed for working on extractions.
        # logs data only for debug and higher verbosity levels.
        self._sanitize_and_log(
            data,
            # these fields are here because they are often very lengthy.
            # they could be useful to others, devs should change redact_fields
            # as needed, but maybe not commit these changes
            redact_fields=["automatic_captions", "formats", "heatmap"],
        )
        """
        return YtdlpResponseDict(data)

    async def _filtered_extract_info(
        self, song_subject: str, *args: Any, **kwargs: Any
    ) -> Dict[str, Any]:
        """
        The real logic behind Downloader.extract_info()
        This function uses an event loop executor to make the call to
        YoutubeDL.extract_info() via the unsafe instance, which will issue errors.

        :param: song_subject: a song url or search subject.
        :kwparam: as_stream: If we should try to queue the URL anyway and let ffmpeg figure it out.

        :returns: Dictionary of data returned from extract_info() or other
            integration. Serialization ready.

        :raises: musicbot.exceptions.MusicbotError
            if event loop is closed and cannot be used for extraction.

        :raises: musicbot.exceptions.ExtractionError
            for errors in MusicBot's internal filtering and pre-processing of extraction queries.

        :raises: musicbot.exceptions.SpotifyError
            for issues with Musicbot's Spotify API request and data handling.

        :raises: yt_dlp.utils.YoutubeDLError
            as a base exception for any exceptions raised by yt_dlp.

        :raises: yt_dlp.networking.exceptions.RequestError
            as a base exception for any networking errors raised by yt_dlp.
        """
        log.noise(  # type: ignore[attr-defined]
            "Called extract_info with:  '%(subject)s', %(args)s, %(kws)s",
            {"subject": song_subject, "args": args, "kws": kwargs},
        )
        as_stream_url = kwargs.pop("as_stream", False)

        # check if loop is closed and exit.
        if (self.bot.loop and self.bot.loop.is_closed()) or not self.bot.loop:
            log.warning(
                "Cannot run extraction, loop is closed. (This is normal on shutdowns.)"
            )
            raise MusicbotException("Cannot continue extraction, event loop is closed.")

        # handle extracting spotify links before ytdl get a hold of them.
        if (
            "open.spotify.com" in song_subject.lower()
            and self.bot.config.spotify_enabled
            and self.bot.spotify is not None
        ):
            if not Spotify.is_url_supported(song_subject):
                raise ExtractionError("Spotify URL is invalid or not supported.")

            process = bool(kwargs.get("process", True))
            download = kwargs.get("download", True)

            # return only basic ytdl-flavored data from the Spotify API.
            # This call will not fetch all tracks in playlists or albums.
            if not process and not download:
                data = await self.bot.spotify.get_spotify_ytdl_data(song_subject)
                return data

            # modify args to have ytdl return search data, only for singular tracks.
            # for albums & playlists, we want to return full playlist data rather than partial as above.
            if process:
                data = await self.bot.spotify.get_spotify_ytdl_data(
                    song_subject, process
                )
                if data["_type"] == "url":
                    song_subject = f"{self.bot.config.default_search_service}:"
                    song_subject += data["search_terms"]
                elif data["_type"] == "playlist":
                    return data

        # B14: YouTube's top search hit can be a channel, and a one-result
        # search ("ytsearch:") then queued the whole channel as one track.
        # Ask for a few results (from the same results page) and keep the
        # first video, below.
        single_yt_search = song_subject.startswith("ytsearch:") and kwargs.get(
            "process", True
        )
        if single_yt_search:
            song_subject = "ytsearch5:" + song_subject[len("ytsearch:") :]

        # Actually call YoutubeDL extract_info.
        try:
            data = await self.bot.loop.run_in_executor(
                self.thread_pool,
                functools.partial(
                    self._extract_in_thread, False, song_subject, *args, **kwargs
                ),
            )
        except DownloadError as e:
            if not as_stream_url:
                raise ExtractionError(
                    "Error in yt-dlp while downloading media data: %(raw_error)s",
                    fmt_args={"raw_error": e},
                ) from e

            log.exception("Download Error with stream URL")
            if e.exc_info[0] == UnsupportedError:
                # ytdl doesn't support it but it could be stream-able...
                song_url = self.get_url_or_none(song_subject)
                if song_url:
                    log.debug("Assuming content is a direct stream")
                    data = {
                        "title": song_subject,
                        "extractor": None,
                        "url": song_url,
                        "__force_stream": True,
                    }
                else:
                    raise ExtractionError("Cannot stream an invalid URL.") from e

            else:
                raise ExtractionError(
                    "Error in yt-dlp while downloading stream data: %(raw_error)s",
                    fmt_args={"raw_error": e},
                ) from e
        except NoSupportingHandlers:
            # due to how we allow search service strings we can't just encode this by default.
            # on the other hand, this method prevents cmd_stream from taking search strings.
            log.noise(  # type: ignore[attr-defined]
                "Caught NoSupportingHandlers, trying again after replacing colon with space."
            )
            song_subject = song_subject.replace(":", " ")
            # TODO: maybe this also needs some exception handling...
            data = await self.bot.loop.run_in_executor(
                self.thread_pool,
                functools.partial(
                    self._extract_in_thread, False, song_subject, *args, **kwargs
                ),
            )

        # make sure the ytdlp data is serializable to make it more predictable.
        data = youtube_dl.YoutubeDL.sanitize_info(data)

        # B14: keep one result, the first video (ie_key "Youtube"; channels
        # and playlists are "YoutubeTab"), so the single-result handling
        # below applies as before.
        if single_yt_search and isinstance(data.get("entries"), list):
            results = data["entries"]
            videos = [e for e in results if e and e.get("ie_key") == "Youtube"]
            if results and videos and videos[0] is not results[0]:
                log.info(
                    "Top search result is not a video (%(url)s), using the first video:  %(title)s",
                    {"url": results[0].get("url"), "title": videos[0].get("title")},
                )
            data["entries"] = videos[:1] or results[:1]
            data["playlist_count"] = len(data["entries"])

        # Extractor youtube:search returns a playlist-like result, usually with one entry
        # when searching via a play command.
        # Combine the entry dict with the info dict as if it was a top-level extraction.
        # This prevents single-entry searches being processed like a playlist later.
        # However we must preserve the list behavior when using cmd_search.
        if (
            check_extractor(data.get("extractor", ""), "search")
            and len(data.get("entries", [])) == 1
            and isinstance(data.get("entries", None), list)
            and data.get("playlist_count", 0) == 1
            and any(song_subject.startswith(e) for e in self._supported_search)
        ):
            log.noise(  # type: ignore[attr-defined]
                "Extractor %(extractor)s returned single-entry result, replacing base info with entry info.",
                {"extractor": data.get("extractor", "__unknown__")},
            )
            entry_info = copy.deepcopy(data["entries"][0])
            for key in entry_info:
                data[key] = entry_info[key]

        return data

    async def safe_extract_info(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        """
        Awaits an event loop executor to call extract_info in a thread pool.
        Uses an instance of YoutubeDL with errors explicitly ignored to
        call extract_info with all arguments passed to this function.
        """
        log.noise(  # type: ignore[attr-defined]
            "Called safe_extract_info with:  %(args)s, %(kws)s",
            {"args": args, "kws": kwargs},
        )
        return await self.bot.loop.run_in_executor(
            self.thread_pool,
            functools.partial(self._extract_in_thread, True, *args, **kwargs),
        )

    def _return_local_media(self, song_subject: str) -> "YtdlpResponseDict":
        """Verifies local media files and returns suitable data for local entries."""
        filename = song_subject[7:]
        media_path = self.bot.config.media_file_dir.resolve()
        unclean_path = media_path.joinpath(filename).resolve()
        if os.path.commonprefix([media_path, unclean_path]) == str(media_path):
            local_file_path = unclean_path
        else:
            local_file_path = media_path.joinpath(pathlib.Path(filename).name)
        corrected_fie_uri = str(local_file_path).replace(str(media_path), "file://")

        if not local_file_path.is_file():
            raise MusicbotException("The local media file could not be found.")

        return YtdlpResponseDict(
            {
                "__input_subject": song_subject,
                "__header_data": None,
                "__expected_filename": str(local_file_path),
                "_type": "local",
                # "original_url": song_subject,
                "extractor": "local:musicbot",
                "extractor_key": "LocalMediaMusicBot",
                # Getting a "good" title for the track will take some serious consideration...
                "title": local_file_path.name,
                "url": corrected_fie_uri,
            }
        )

    async def _return_autoplaylist(self, song_subject: str) -> "YtdlpResponseDict":
        """Converts an autoplaylist file into a usable playlist result."""
        plname = song_subject[8:]
        if not self.bot.playlist_mgr.playlist_exists(plname):
            raise MusicbotException("The playlist does not exist.")

        pl = self.bot.playlist_mgr.get_playlist(plname)
        if not pl.loaded:
            await pl.load()

        # terms used to trigger URL processing.
        pterms = ["playlist", "bandcamp.com/album"]

        # process each playlist entries.
        entries_data: List[Dict[str, Any]] = []
        for track in pl:
            if not self.bot.loop or (self.bot.loop and self.bot.loop.is_closed()):
                return YtdlpResponseDict({})
            if self.bot.logout_called:
                return YtdlpResponseDict({})

            # If the track is already a URL, maybe skip it...
            # Some URLs, like playlists, may want to be extracted here, instead.
            # This method will prevent queue estimations.
            song_url = self.get_url_or_none(track)
            if song_url and all(x not in song_url.lower() for x in pterms):
                entries_data.append(
                    {
                        "_type": "url",
                        "extractor": "autoplaylist:musicbot",
                        "extractor_key": "AutoPlaylistEntry",
                        "__header_data": None,
                        "__input_subject": track,
                        "url": song_url,
                    }
                )
                continue

            # extract with ytdlp
            try:
                info = await self.extract_info(track, download=False, process=True)
            except (
                youtube_dl.utils.YoutubeDLError,
                youtube_dl.utils.DownloadError,
            ) as e:
                log.error(
                    'Error while processing song "%(url)s":  %(raw_error)s',
                    {"url": track, "raw_error": e},
                )
                continue

            except ExtractionError as e:
                log.error(
                    'Error extracting song "%(url)s": %(raw_error)s',
                    {
                        "url": track,
                        "raw_error": _L(e.message) % e.fmt_args,
                    },
                    exc_info=True,
                )
                continue

            except MusicbotException as e:
                if track.lower().startswith("file://"):
                    log.error(
                        "Could not process track '%(track)s' due to: %(raw_error)s",
                        {
                            "track": track,
                            "raw_error": _L(e.message) % e.fmt_args,
                        },
                    )
                    continue
                # else, just bail.
                log.exception(
                    "MusicBot needs to stop the auto playlist extraction and bail."
                )
                break
            except Exception:  # pylint: disable=broad-exception-caught
                log.exception(
                    "MusicBot got an unhandled exception while adding auto playlist to the queue."
                )
                break

            # Process playlists
            if info.has_entries:
                entries_data.extend(info.get_entries_dicts())

            # process single entries.
            else:
                entries_data.append(info.data.copy())

        return YtdlpResponseDict(
            {
                "__input_subject": f"apl://{pl.filename}",
                "__header_data": None,
                "_type": "playlist",
                "extractor": "autoplaylist:musicbot",
                "extractor_key": "AutoPlaylist",
                "entries": entries_data,
                "playlist_count": len(entries_data),
            }
        )


class YtdlpResponseDict(YUserDict):
    """
    UserDict wrapper for ytdlp extraction data with helpers for easier data reuse.
    The dict features are available only for compat with existing code.
    Use of the dict subscript notation is not advised and could/should be
    removed in the future, in favor of typed properties and processing
    made available herein.

    See ytdlp doc string in InfoExtractor for info on data:
    https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/extractor/common.py
    """

    def __init__(self, data: Dict[str, Any]) -> None:
        super().__init__(data)
        self._propagate_entry_data()

    def _propagate_entry_data(self) -> None:
        """ensure the `__input_subject` key is set on all child entries."""
        subject = self.get("__input_subject", None)
        if not subject:
            log.warning("Missing __input_subject from YtdlpResponseDict")

        entries = self.data.get("entries", [])
        if not isinstance(entries, list):
            log.warning(
                "Entries is not a list in YtdlpResponseDict, set process=True to avoid this."
            )
            return

        for entry in self.data.get("entries", []):
            if "__input_subject" not in entry:
                entry["__input_subject"] = subject

    def get_entries_dicts(self) -> List[Dict[str, Any]]:
        """will return entries as-is from data or an empty list if no entries are set."""
        entries = self.data.get("entries", [])
        if isinstance(entries, list):
            return entries
        return []

    def get_entries_objects(self) -> List["YtdlpResponseDict"]:
        """will iterate over entries and return list of YtdlpResponseDicts"""
        return [YtdlpResponseDict(e) for e in self.get_entries_dicts()]

    def get_entry_dict_at(self, idx: int) -> Optional[Dict[str, Any]]:
        """Get a dict from "entries" at the given index or None."""
        entries = self.get_entries_dicts()
        if entries:
            try:
                return entries[idx]
            except IndexError:
                pass
        return None

    def get_entry_object_at(self, idx: int) -> Optional["YtdlpResponseDict"]:
        """Get a YtdlpResponseDict for given entry or None."""
        e = self.get_entry_dict_at(idx)
        if e:
            return YtdlpResponseDict(e)
        return None

    def get_playable_url(self) -> str:
        """
        Get a playable URL for any given response type.
        will try 'url', then 'webpage_url'
        """
        if self.ytdl_type == "video":
            if not self.webpage_url:
                return self.url
            return self.webpage_url

        if not self.url:
            return self.webpage_url
        return self.url

    def http_header(self, header_name: str, default: Any = None) -> Any:
        """Get HTTP Header information if it is available."""
        headers = self.data.get("__header_data", None)
        if headers:
            return headers.get(
                header_name.upper(),
                default,
            )
        return default

    @property
    def input_subject(self) -> str:
        """Get the input subject used to create this data."""
        subject = self.data.get("__input_subject", "")
        if isinstance(subject, str):
            return subject
        return ""

    @property
    def expected_filename(self) -> Optional[str]:
        """get expected filename for this info data, or None if not available"""
        fn = self.data.get("__expected_filename", None)
        if isinstance(fn, str) and fn:
            return fn
        return None

    @property
    def entry_count(self) -> int:
        """count of existing entries if available or 0"""
        if self.has_entries:
            return len(self.data["entries"])
        return 0

    @property
    def has_entries(self) -> bool:
        """bool status if iterable entries are present."""
        if "entries" not in self.data:
            return False
        if not isinstance(self.data["entries"], list):
            return False
        return bool(len(self.data["entries"]))

    @property
    def thumbnail_url(self) -> str:
        """
        Get a thumbnail url if available, or create one if possible, otherwise returns an empty string.
        Note, the URLs returned from this function may be time-sensitive.
        In the case of spotify, URLs may not last longer than a day.
        """
        turl = self.data.get("thumbnail", None)
        # if we have a thumbnail url, clean it up if needed and return it.
        if isinstance(turl, str) and turl:
            return turl

        # Check if we have a thumbnails key and pick a thumb from it.
        # TODO: maybe loop over these finding the largest / highest priority entry instead?.
        thumbs = self.data.get("thumbnails", [])
        if thumbs:
            if self.extractor.startswith("youtube"):
                # youtube seems to set the last list entry to the largest.
                turl = thumbs[-1].get("url")

            # spotify images are in size descending order, reverse of youtube.
            elif len(thumbs):
                turl = thumbs[0].get("url")

            if isinstance(turl, str) and turl:
                return turl

        # if all else fails, try to make a URL on our own.
        if self.extractor.startswith("youtube"):
            if self.video_id:
                return f"https://i.ytimg.com/vi/{self.video_id}/maxresdefault.jpg"

        # Extractor Bandcamp:album unfortunately does not give us thumbnail(s)
        # tracks do have thumbs, but process does not return them while "extract_flat" is enabled.
        # we don't get enough data with albums to make a thumbnail URL either.
        # really this is an upstream issue with ytdlp, and should be patched there.
        # See: BandcampAlbumIE and add missing thumbnail: self._og_search_thumbnail(webpage)

        return ""

    @property
    def ytdl_type(self) -> str:
        """returns value for data key '_type' or empty string"""
        t = self.data.get("_type", "")
        if isinstance(t, str) and t:
            return t
        return ""

    @property
    def extractor(self) -> str:
        """returns value for data key 'extractor' or empty string"""
        e = self.data.get("extractor", "")
        if isinstance(e, str) and e:
            return e
        return ""

    @property
    def extractor_key(self) -> str:
        """returns value for data key 'extractor_key' or empty string"""
        ek = self.data.get("extractor_key", "")
        if isinstance(ek, str) and ek:
            return ek
        return ""

    @property
    def url(self) -> str:
        """returns value for data key 'url' or empty string"""
        u = self.data.get("url", "")
        if isinstance(u, str) and u:
            return u
        return ""

    @property
    def webpage_url(self) -> str:
        """returns value for data key 'webpage_url' or None"""
        u = self.data.get("webpage_url", "")
        if isinstance(u, str) and u:
            return u
        return ""

    @property
    def webpage_basename(self) -> Optional[str]:
        """returns value for data key 'webpage_url_basename' or None"""
        bn = self.data.get("webpage_url_basename", None)
        if isinstance(bn, str) and bn:
            return bn
        return None

    @property
    def webpage_domain(self) -> Optional[str]:
        """returns value for data key 'webpage_url_domain' or None"""
        d = self.data.get("webpage_url_domain", None)
        if isinstance(d, str) and d:
            return d
        return None

    @property
    def original_url(self) -> Optional[str]:
        """returns value for data key 'original_url' or None"""
        u = self.data.get("original_url", None)
        if isinstance(u, str) and u:
            return u
        return None

    @property
    def video_id(self) -> str:
        """returns the id if it exists or empty string."""
        i = self.data.get("id", "")
        if isinstance(i, str) and i:
            return i
        return ""

    @property
    def title(self) -> str:
        """returns title value if it exists, empty string otherwise."""
        # Note: seemingly all processed data should have "title" key
        # entries in data may also have "fulltitle" and "playlist_title" keys.
        t = self.data.get("title", "")
        if isinstance(t, str) and t:
            return t
        return ""

    @property
    def playlist_count(self) -> int:
        """returns the playlist_count value if it exists, or 0"""
        c = self.data.get("playlist_count", 0)
        if isinstance(c, int):
            return c
        return 0

    @property
    def duration(self) -> float:
        """returns duration in seconds if available, or 0"""
        try:
            return float(self.data.get("duration", 0))
        except (ValueError, TypeError):
            log.noise(  # type: ignore[attr-defined]
                "Warning, duration error for: %(url)s",
                {"url": self.original_url},
                exc_info=True,
            )
            return 0.0

    @property
    def duration_td(self) -> datetime.timedelta:
        """
        Returns duration as a datetime.timedelta object.
        May contain 0 seconds duration.
        """
        t = self.duration or 0
        return datetime.timedelta(seconds=t)

    @property
    def is_live(self) -> bool:
        """return is_live key status or False if not found."""
        # Can be true, false, or None if state is unknown.
        # Here we assume unknown is not live.
        live = self.data.get("is_live", False)
        if isinstance(live, bool):
            return live
        return False

    @property
    def is_stream(self) -> bool:
        """indicate if this response is a streaming resource."""
        if self.is_live:
            return True

        # So live_status can be one of:
        # None (=unknown), 'is_live', 'is_upcoming', 'was_live', 'not_live',
        # or 'post_live' (was live, but VOD is not yet processed)
        # We may want to add is_upcoming, and post_live to this check but I have not been able to test it.
        if self.data.get("live_status", "") == "is_live":
            return True

        # Warning: questionable methods from here on.
        if self.extractor.startswith("generic"):
            # check against known streaming service headers.
            if self.http_header("ICY-NAME") or self.http_header("ICY-URL"):
                return True

            # hacky not good way to last ditch it.
            # url = self.url.lower()
            # if "live" in url or "stream" in url:
            #    return True

        return False
