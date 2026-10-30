"""One durable database, one connection per thread.

The workstation serves pages from request threads and runs its strategy loops on
their own worker threads.  A :class:`ForexRepository` owns a SQLite connection,
and SQLite connections belong to the thread that opened them, so passing one
repository object across threads would be a bug that only shows up under load.

:class:`ThreadLocalRepository` hands each thread its own connection to the same
file and delegates everything else.  Concurrency is resolved where it belongs --
inside SQLite -- because every write already takes ``BEGIN IMMEDIATE`` under a
30-second busy timeout, so two threads queue instead of interleaving.  Nothing
here weakens that: it only stops two threads from sharing one connection object.

The proxy exposes no new capability: a caller that could not read or write before
cannot now.
"""

from __future__ import annotations

import threading
from typing import Any, Callable


class ThreadLocalRepository:
    """A per-thread :class:`ForexRepository` for one database file."""

    def __init__(
        self,
        database_path: str,
        *,
        factory: Callable[[str], Any] | None = None,
    ) -> None:
        if not isinstance(database_path, str) or not database_path.strip():
            raise ValueError("explicit_database_path_required")
        self.database_path = database_path
        if factory is None:
            from .repository import ForexRepository

            factory = ForexRepository
        self._factory = factory
        self._local = threading.local()

    def _repo(self) -> Any:
        repo = getattr(self._local, "repo", None)
        if repo is None:
            repo = self._factory(self.database_path)
            self._local.repo = repo
        return repo

    # -- delegation ------------------------------------------------------

    def __getattr__(self, name: str) -> Any:
        # Guard against recursion during unpickling / attribute probing.
        if name in {"_local", "_factory", "database_path"}:
            raise AttributeError(name)
        return getattr(self._repo(), name)

    #: ``ThreadLocalRepository.connection`` is the calling thread's connection.
    @property
    def connection(self) -> Any:
        return self._repo().connection

    def close(self) -> None:
        """Close *this thread's* connection.  Other threads keep theirs."""

        repo = getattr(self._local, "repo", None)
        if repo is not None:
            repo.close()
            self._local.repo = None

    def close_all(self) -> None:
        self.close()


__all__ = ["ThreadLocalRepository"]
