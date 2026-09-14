import aiosqlite

from settings import DATABASE_PATH


async def init_db():
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(str(DATABASE_PATH)) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id        TEXT PRIMARY KEY,
                name           TEXT NOT NULL DEFAULT '',
                role           TEXT NOT NULL DEFAULT '',
                description    TEXT NOT NULL DEFAULT '',
                created_at     TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        # migration: add name column for older databases that lack it
        try:
            await db.execute("ALTER TABLE users ADD COLUMN name TEXT NOT NULL DEFAULT ''")
        except Exception:
            pass
        # migration: add role column for older databases that lack it
        try:
            await db.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT ''")
        except Exception:
            pass
        # migration: add description column for older databases that lack it
        try:
            await db.execute("ALTER TABLE users ADD COLUMN description TEXT NOT NULL DEFAULT ''")
        except Exception:
            pass
        await db.commit()


async def get_db():
    async with aiosqlite.connect(str(DATABASE_PATH)) as db:
        db.row_factory = aiosqlite.Row
        yield db