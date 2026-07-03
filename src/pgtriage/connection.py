"""PostgreSQL connection manager with read-only enforcement."""

import psycopg
from psycopg.rows import dict_row


class ConnectionManager:
    def __init__(self, connection_string: str):
        self._conn_string = connection_string
        self._conn: psycopg.AsyncConnection | None = None

    async def connect(self) -> None:
        self._conn = await psycopg.AsyncConnection.connect(
            self._conn_string,
            autocommit=True,
            row_factory=dict_row,
        )
        await self._conn.execute(
            "SET default_transaction_read_only = true"
        )

    async def close(self) -> None:
        if self._conn and not self._conn.closed:
            await self._conn.close()

    async def _ensure_connected(self) -> None:
        if self._conn is None or self._conn.closed:
            await self.connect()

    @property
    def conn(self) -> psycopg.AsyncConnection:
        if self._conn is None or self._conn.closed:
            raise RuntimeError("Database connection is not established")
        return self._conn

    async def fetch_all(self, query: str, params: tuple = ()) -> list[dict]:
        await self._ensure_connected()
        async with self.conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(query, params)
            return await cur.fetchall()

    async def fetch_one(self, query: str, params: tuple = ()) -> dict | None:
        await self._ensure_connected()
        async with self.conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(query, params)
            return await cur.fetchone()

    async def get_server_version(self) -> str:
        row = await self.fetch_one("SELECT version() AS version")
        return row["version"] if row else "unknown"

    async def has_extension(self, name: str) -> bool:
        row = await self.fetch_one(
            "SELECT EXISTS(SELECT 1 FROM pg_extension WHERE extname = %s) AS available",
            (name,),
        )
        return row["available"] if row else False
