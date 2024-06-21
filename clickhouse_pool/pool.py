"""Connection pool for clickhouse_driver

Heavily inspired by psycopg2/lib/pool.py, this module implements a thread-safe
connection pool. Each connection is an instance of a clickhouse_driver client.
"""
import threading
from contextlib import contextmanager
from clickhouse_driver import Client as TCPClient
from clickhouse_connect import get_client
from typing import Generator, Any
from enum import Enum

class ClientDriver(Enum):
    """Enum for the different types of clients available."""
    TCP = 1
    HTTP = 2
    
def CHClientFactory(driver: ClientDriver, **kwargs) -> Any:
    """Factory function for creating a clickhouse client.

    Args:
        driver: The type of client to create.
        **kwargs: similar to clickhouse-driver, all settings available are
            documented `here
            <https://clickhouse.tech/docs/en/single/#settings>`_.
    """
    if driver == ClientDriver.TCP:
        return TCPClient(**kwargs)
    elif driver == ClientDriver.HTTP:
        return get_client(**kwargs)


class ChPoolError(Exception):
    """A generic exception that may be raised by ChPool"""


class TooManyConnections(ChPoolError):
    """Raised when attempting to use more than connections_max clients."""


class ChPool:
    """A ChPool is a pool of connections (Clients) to a ClickhouseServer.

    Attributes:
        connections_min (int): minimum number of connections to keep open
        connections_max (int): maximum number of connections allowed open
        closed (bool): if closed the pool has no connections and cannot be used
    """

    # pylint: disable=too-many-instance-attributes
    def __init__(self, **kwargs):
        """Initialize the pool of clickhouse clients.

        Args:
            **kwargs: similar to clickhouse-driver, all settings available are
                documented `here
                <https://clickhouse.tech/docs/en/single/#settings>`_.
        """

        self.connections_min = kwargs.pop("connections_min", 10)
        self.connections_max = kwargs.pop("connections_max", 20)

        self.connection_args = {"host": kwargs.pop("host", "localhost"), **kwargs}
        self.closed = False
        self._pool = []
        self._used = {}

        # similar to psycopg2 pools, _rused is used for mapping instances of conn
        # to their respective keys in _used
        self._rused = {}
        self._keys = 0
        self._lock = threading.Lock()

        for _ in range(self.connections_min):
            self._connect()

    def _connect(self, key: str = None) -> Any:
        """Create a new client and assign to a key."""
        client = CHClientFactory(**self.connection_args)
        if key is not None:
            self._used[key] = client
            self._rused[id(client)] = key
        else:
            self._pool.append(client)
        return client

    def _get_key(self):
        """Get an unused key."""
        self._keys += 1
        return self._keys

    def pull(self, key: str = None) -> Any:
        """Get an available CH Client from the pool.

        Args:
            key: If known, the key of the client you would like.

        Returns:
            A clickhouse-driver client.

        """

        self._lock.acquire()
        try:
            if self.closed:
                raise ChPoolError("pool closed")

            if key is None:
                key = self._get_key()

            if key in self._used:
                return self._used[key]

            if self._pool:
                self._used[key] = client = self._pool.pop()
                self._rused[id(client)] = key
                return client

            if len(self._used) >= self.connections_max:
                raise TooManyConnections("too many connections")
            return self._connect(key)
        finally:
            self._lock.release()

    def push(self, client: Any = None, key: str = None, close: bool = False):
        """Return a client to the pool for reuse.

        Args:
            client: The client to return.
            key: If known, the key of the client.
            close: Close the client instead of adding back to pool.

        """

        self._lock.acquire()
        try:
            if self.closed:
                raise ChPoolError("pool closed")
            if key is None:
                key = self._rused.get(id(client))
                if key is None:
                    raise ChPoolError("trying to put unkeyed client")
            if len(self._pool) < self.connections_min and not close:
                # TODO: verify connection still valid
                if client.connection.connected:
                    self._pool.append(client)
            else:
                client.disconnect()

            # ensure thread doesn't put connection back once the pool is closed
            if not self.closed or key in self._used:
                del self._used[key]
                del self._rused[id(client)]
        finally:
            self._lock.release()

    def cleanup(self):
        """Close all open connections in the pool.

        This method loops through eveery client and calls disconnect.
        """

        self._lock.acquire()
        try:
            if self.closed:
                raise ChPoolError("pool closed")
            for client in self._pool + list(self._used.values()):
                try:
                    client.disconnect()
                # TODO: handle problems with disconnect
                except Exception:
                    pass
            self.closed = True
        finally:
            self._lock.release()

    @contextmanager
    def get_client(self, key: str = None) -> Generator[Any, None, None]:
        """A clean way to grab a client via a contextmanager.


        Args:
            key: If known, the key of the client to grab.

        Yields:
            Abny: a clickhouse-driver client or clickhouse-connect client.

        """
        client = self.pull(key)
        try:
            yield client
        finally:
            self.push(client=client)
