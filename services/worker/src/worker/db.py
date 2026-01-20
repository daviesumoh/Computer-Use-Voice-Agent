from psycopg import AsyncConnection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import AsyncConnectionPool

from worker.settings import Settings

Conn = AsyncConnection[DictRow]
Pool = AsyncConnectionPool[Conn]


def create_pool(settings: Settings) -> Pool:
    """Opened by the app lifespan. Small: one worker process, and Supabase free caps connections."""
    return AsyncConnectionPool(
        settings.database_url.get_secret_value(),
        connection_class=Conn,
        min_size=1,
        max_size=5,
        # Autocommit: statements commit individually; atomicity only via conn.transaction().
        kwargs={"row_factory": dict_row, "autocommit": True},
        open=False,
    )
