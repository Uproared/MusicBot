"""
The audio thread raises its own priority (stutter fix).
"""

import logging
import sys
import threading
from types import SimpleNamespace

import pytest

import musicbot.player as player_mod
from musicbot.player import SourcePlaybackCounter


@pytest.mark.skipif(sys.platform != "win32", reason="Windows thread priorities")
def test_windows_audio_thread_gets_highest_priority():
    seen = []

    def audio_thread():
        player_mod._raise_audio_thread_priority()
        k32 = player_mod._windows_kernel32()
        seen.append(k32.GetThreadPriority(k32.GetCurrentThread()))

    t = threading.Thread(target=audio_thread)
    t.start()
    t.join(5)
    assert seen == [2]  # THREAD_PRIORITY_HIGHEST


@pytest.fixture
def fake_linux(monkeypatch):
    """Pretend to be Linux, with a fake getpriority/setpriority."""
    state = SimpleNamespace(nice=0, calls=[], refuse=False)

    def getpriority(_which, _who):
        return state.nice

    def setpriority(which, who, nice):
        if state.refuse:
            raise PermissionError(13, "Permission denied")
        state.calls.append((which, who, nice))

    monkeypatch.setattr(player_mod, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(
        player_mod,
        "os",
        SimpleNamespace(PRIO_PROCESS=0, getpriority=getpriority, setpriority=setpriority),
    )
    monkeypatch.setattr(player_mod, "get_native_id", lambda: 4321)
    monkeypatch.setattr(player_mod, "_priority_refused_logged", False)
    return state


def test_linux_thread_nice_is_lowered_to_minus_5(fake_linux):
    player_mod._raise_audio_thread_priority()
    assert fake_linux.calls == [(0, 4321, -5)]


def test_linux_never_lowers_a_priority_that_is_already_higher(fake_linux):
    fake_linux.nice = -10  # e.g. Nice=-10 in the systemd unit
    player_mod._raise_audio_thread_priority()
    assert fake_linux.calls == []


def test_refusal_is_logged_once_with_the_fix(fake_linux, caplog):
    caplog.set_level(logging.INFO, logger="musicbot.player")
    fake_linux.refuse = True
    player_mod._raise_audio_thread_priority()
    player_mod._raise_audio_thread_priority()
    msgs = [r.getMessage() for r in caplog.records if "audio thread's priority" in r.getMessage()]
    assert len(msgs) == 1
    assert "LimitNICE=-5" in msgs[0]


def test_each_track_raises_priority_once_on_its_audio_thread(monkeypatch):
    calls = []
    monkeypatch.setattr(player_mod, "_raise_audio_thread_priority", lambda: calls.append(1))

    class Source:
        left = 3

        def read(self):
            self.left -= 1
            return b"x" if self.left >= 0 else b""

        def cleanup(self):
            pass

    counter = SourcePlaybackCounter(Source())
    while counter.read():
        pass
    assert calls == [1]
