"""
Keep one running copy of SetupTTS per user.

Two copies share the same history database, log file and resumable-job
folder.  A real user log showed exactly that (the app started twice within
nine seconds): one copy was closed while the other kept generating, and a
second copy can resume the very job the first is still writing.

The first copy holds a lock file and listens on a local socket.  A second
launch finds the lock taken, asks the first copy to bring its window to the
front, and exits.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from PySide6.QtCore import QLockFile, QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

logger = logging.getLogger(__name__)

_ACTIVATE = b"activate"


class SingleInstance(QObject):
    """Lock + local server.  Call :meth:`acquire` once, early in startup."""

    #: Emitted in the running copy when another launch asks it to show itself.
    activation_requested = Signal()

    def __init__(self, data_dir: Path, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._lock = QLockFile(str(data_dir / "setuptts.lock"))
        # 0 = never stale by age.  A lock left by a crashed copy is still
        # detected (its process no longer exists) and taken over.
        self._lock.setStaleLockTime(0)
        digest = hashlib.sha1(str(data_dir.resolve()).encode("utf-8")).hexdigest()[:12]
        self._server_name = f"SetupTTS-{digest}"
        self._server: QLocalServer | None = None

    def acquire(self) -> bool:
        """True if this is the only running copy (and it now owns the lock)."""
        if not self._lock.tryLock(200):
            return False
        # A socket file can outlive a crashed copy on macOS/Linux.
        QLocalServer.removeServer(self._server_name)
        self._server = QLocalServer(self)
        if not self._server.listen(self._server_name):
            # Not fatal: the lock alone still prevents a second copy.
            logger.warning("Single-instance server unavailable: %s",
                           self._server.errorString())
            self._server = None
        else:
            self._server.newConnection.connect(self._on_connection)
        return True

    def notify_running_instance(self) -> bool:
        """Ask the copy that holds the lock to bring its window forward."""
        socket = QLocalSocket()
        socket.connectToServer(self._server_name)
        if not socket.waitForConnected(1000):
            return False
        socket.write(_ACTIVATE)
        socket.flush()
        socket.waitForBytesWritten(1000)
        # Let the running copy read the message and hang up first.  On
        # Windows a named-pipe client that disconnects straight after writing
        # can lose the data before the server has read it.
        socket.waitForDisconnected(2000)
        socket.abort()
        return True

    def release(self) -> None:
        if self._server is not None:
            self._server.close()
            self._server = None
        self._lock.unlock()

    def _on_connection(self) -> None:
        # The server owns the sockets it hands out and deletes them with
        # itself.  No deleteLater() here: if the server (or this object) is
        # destroyed first, a still-pending deferred delete frees the socket a
        # second time and crashes the process.
        server = self._server
        if server is None:
            return
        while server.hasPendingConnections():
            conn = server.nextPendingConnection()
            if conn is None:
                break
            conn.readyRead.connect(lambda c=conn: self._on_ready(c))
            # On Windows (named pipes) the message has usually arrived before
            # this handler runs; readyRead has then already fired and will
            # not fire again, so read what is buffered right away.
            if conn.bytesAvailable() > 0:
                self._on_ready(conn)

    def _on_ready(self, conn: QLocalSocket) -> None:
        if conn.property("setuptts_handled"):
            return
        data = bytes(conn.readAll().data())
        if data.startswith(_ACTIVATE):
            conn.setProperty("setuptts_handled", True)
            conn.disconnectFromServer()   # acknowledges: the client waits for this
            self.activation_requested.emit()
