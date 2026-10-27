import os
from datetime import datetime
from typing import AsyncIterator, List, Optional

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.ext.asyncio import (
    AsyncAttrs,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _build_database_url() -> str:
    url = os.getenv(
        "DATABASE_URL",
        "postgresql+asyncpg://postgres:Jarrah852003@localhost:5432/telematics",
    )
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+asyncpg://", 1)
    elif url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


DATABASE_URL = _build_database_url()

engine = create_async_engine(
    DATABASE_URL,
    echo=os.getenv("SQL_ECHO", "false").lower() == "true",
    pool_size=int(os.getenv("DB_POOL_SIZE", "10")),
    max_overflow=int(os.getenv("DB_MAX_OVERFLOW", "20")),
    pool_timeout=int(os.getenv("DB_POOL_TIMEOUT", "30")),
    pool_recycle=int(os.getenv("DB_POOL_RECYCLE", "1800")),
    pool_pre_ping=True,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=True,
)


class Base(AsyncAttrs, DeclarativeBase):
    pass


class Driver(Base):
    __tablename__ = "drivers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    plate_no: Mapped[Optional[str]] = mapped_column(String(50), nullable=True, index=True)
    car_type: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    registered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    total_trips: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    current_collective_score: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0, server_default=text("0")
    )

    trips: Mapped[List["Trip"]] = relationship(
        back_populates="driver",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        CheckConstraint("total_trips >= 0", name="ck_drivers_total_trips_nonneg"),
        CheckConstraint(
            "current_collective_score >= 0 AND current_collective_score <= 100",
            name="ck_drivers_score_range",
        ),
    )


class Trip(Base):
    __tablename__ = "trips"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    driver_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("drivers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    distance_km: Mapped[float] = mapped_column(Float, nullable=False)
    speed_score: Mapped[float] = mapped_column(Float, nullable=False)
    brake_score: Mapped[float] = mapped_column(Float, nullable=False)
    corner_score: Mapped[float] = mapped_column(Float, nullable=False)
    collective_score: Mapped[float] = mapped_column(Float, nullable=False)

    driver: Mapped["Driver"] = relationship(back_populates="trips")
    coordinates: Mapped[List["Coordinate"]] = relationship(
        back_populates="trip",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by=lambda: [Coordinate.timestamp, Coordinate.id],
    )
    harsh_events: Mapped[List["HarshEvent"]] = relationship(
        back_populates="trip",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by=lambda: [HarshEvent.timestamp, HarshEvent.id],
    )

    __table_args__ = (
        UniqueConstraint("driver_id", "start_time", name="uq_trips_driver_start_time"),
        Index("ix_trips_driver_id_start_time", "driver_id", "start_time"),
        CheckConstraint("end_time > start_time", name="ck_trips_time_window"),
        CheckConstraint("distance_km >= 0", name="ck_trips_distance_nonneg"),
        CheckConstraint(
            "speed_score >= 0 AND speed_score <= 100", name="ck_trips_speed_score_range"
        ),
        CheckConstraint(
            "brake_score >= 0 AND brake_score <= 100", name="ck_trips_brake_score_range"
        ),
        CheckConstraint(
            "corner_score >= 0 AND corner_score <= 100", name="ck_trips_corner_score_range"
        ),
        CheckConstraint(
            "collective_score >= 0 AND collective_score <= 100",
            name="ck_trips_collective_score_range",
        ),
    )


class HarshEvent(Base):
    __tablename__ = "harsh_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    trip_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("trips.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    g_force: Mapped[float] = mapped_column(Float, nullable=False)

    trip: Mapped["Trip"] = relationship(back_populates="harsh_events")

    __table_args__ = (
        Index("ix_harsh_events_trip_id_timestamp", "trip_id", "timestamp"),
        CheckConstraint("g_force >= 0", name="ck_harsh_events_g_force_nonneg"),
        CheckConstraint(
            "latitude >= -90 AND latitude <= 90", name="ck_harsh_events_latitude_range"
        ),
        CheckConstraint(
            "longitude >= -180 AND longitude <= 180", name="ck_harsh_events_longitude_range"
        ),
    )


class Coordinate(Base):
    __tablename__ = "coordinates"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    trip_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("trips.id", ondelete="CASCADE"), nullable=False
    )
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    speed_kmh: Mapped[float] = mapped_column(Float, nullable=False)

    trip: Mapped["Trip"] = relationship(back_populates="coordinates")

    __table_args__ = (
        Index("ix_coordinates_trip_id_timestamp", "trip_id", "timestamp"),
        CheckConstraint("speed_kmh >= 0", name="ck_coordinates_speed_nonneg"),
        CheckConstraint(
            "latitude >= -90 AND latitude <= 90", name="ck_coordinates_latitude_range"
        ),
        CheckConstraint(
            "longitude >= -180 AND longitude <= 180", name="ck_coordinates_longitude_range"
        ),
    )


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a request-scoped async session."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def init_models() -> None:
    """Create all tables if they do not exist. Use Alembic for managed migrations."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def dispose_engine() -> None:
    await engine.dispose()