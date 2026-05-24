import asyncio
import os
from pathlib import Path

import asyncpg


async def main():
    sql_path = Path(__file__).resolve().parents[2] / "db-migrations" / "004_web_admin.sql"
    if not sql_path.exists():
        sql_path = Path(__file__).resolve().parent / "004_web_admin.sql"
    sql = sql_path.read_text()
    conn = await asyncpg.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ.get("DB_PORT", "5432")),
        database=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        ssl="require",
    )
    try:
        await conn.execute(sql)
        print("004_web_admin migration applied")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
