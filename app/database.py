from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


engine = create_engine(
    settings.database_url,
    connect_args={'check_same_thread': False},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db() -> None:
    from . import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    migrate_probe_scopes(engine)


def migrate_probe_scopes(bind) -> None:
    """Add probe metadata to existing SQLite databases, retaining IDs/history."""
    additions = {
        "targets": {"tcp_port": f"INTEGER NOT NULL DEFAULT {settings.tcp_port}"},
        "topology_snapshots": {
            "protocol": "VARCHAR(8) NOT NULL DEFAULT 'icmp'",
            "destination_port": "INTEGER NOT NULL DEFAULT 0",
        },
        "route_paths": {
            "protocol": "VARCHAR(8) NOT NULL DEFAULT 'icmp'",
            "destination_port": "INTEGER NOT NULL DEFAULT 0",
            "endpoint_response": "VARCHAR(40)",
        },
    }
    with bind.begin() as connection:
        inspector = inspect(connection)
        for table, fields in additions.items():
            columns = {column["name"] for column in inspector.get_columns(table)}
            for name, definition in fields.items():
                if name not in columns:
                    connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
