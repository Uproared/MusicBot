import asyncio
import collections
import inspect
import logging
from typing import TYPE_CHECKING, Any, Callable, DefaultDict, List, Set

if TYPE_CHECKING:
    AsyncTask = asyncio.Task[Any]
else:
    AsyncTask = asyncio.Task

log = logging.getLogger(__name__)

EventCallback = Callable[..., Any]
EventList = List[EventCallback]
EventDict = DefaultDict[str, EventList]


class EventEmitter:
    def __init__(self) -> None:
        """
        Manage a collection of events and an event loop to run callbacks
        that are also co-routines.
        """
        self._events: EventDict = collections.defaultdict(list)
        self.loop: asyncio.AbstractEventLoop = asyncio.get_event_loop()
        self._task_pool: Set[AsyncTask] = set()

    def emit(self, event: str, *args: Any, **kwargs: Any) -> None:
        """
        Trigger all callbacks registered with `event` to be fired using
        the arguments passed to this function.
        """
        if event not in self._events:
            return

        for cb in list(self._events[event]):
            try:
                if inspect.iscoroutinefunction(cb):
                    # Create the task and save a reference to ensure it is not
                    # garbage collected early.
                    t = self.loop.create_task(
                        cb(*args, **kwargs),
                        name=f"MB_EE_{type(self).__name__}_{cb.__name__}",
                    )
                    self._task_pool.add(t)
                    t.add_done_callback(self._handler_done)
                else:
                    cb(*args, **kwargs)

            except Exception:  # pylint: disable=broad-exception-caught
                # print_exc() only reached the console, not the log file (S5).
                log.exception("Error in %s event handler %s", event, cb.__name__)

    def _handler_done(self, task: AsyncTask) -> None:
        """
        Release an event handler task and log its exception, if any. Without
        this, a failing handler (e.g. on_player_play) only surfaced at garbage
        collection as "Task exception was never retrieved", if at all (S5).
        """
        self._task_pool.discard(task)
        if task.cancelled():
            return
        ex = task.exception()
        if ex is not None:
            log.error("Error in event handler task %s", task.get_name(), exc_info=ex)

    def on(self, event: str, callback: EventCallback) -> Any:
        """
        Schedule a `callback` to run each time `event` is emitted.
        """
        self._events[event].append(callback)
        return self

    def off(self, event: str, callback: EventCallback) -> Any:
        """
        Remove a scheduled `callback` from the `event` register.
        """
        self._events[event].remove(callback)

        if not self._events[event]:
            del self._events[event]

        return self

    def once(self, event: str, callback: EventCallback) -> Any:
        """
        Schedule a `callback` function to fire only once, the first time
        an `even` gets fired.
        """

        def callback_off(*args: Any, **kwargs: Any) -> Any:
            self.off(event, callback_off)
            return callback(*args, **kwargs)

        return self.on(event, callback_off)
