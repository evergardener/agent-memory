from contextlib import contextmanager

from psycopg_pool import ConnectionPool

from .config import Settings


class Database:
    def __init__(self, settings: Settings):
        self.pool = ConnectionPool(settings.database_url, min_size=1, max_size=10, open=False)

    def open(self) -> None:
        self.pool.open(wait=True)

    def close(self) -> None:
        self.pool.close()

    @contextmanager
    def connection(self):
        with self.pool.connection() as connection:
            yield connection
