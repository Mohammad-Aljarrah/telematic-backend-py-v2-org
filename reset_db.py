import asyncio
from database import engine, Base

async def reset_database():
    async with engine.begin() as conn:
        print("Dropping all existing tables...")
        await conn.run_sync(Base.metadata.drop_all)
        print("Recreating tables with the new schema...")
        await conn.run_sync(Base.metadata.create_all)
    print("Database successfully reset!")
    await engine.dispose()

if __name__ == "__main__":
    asyncio.run(reset_database())