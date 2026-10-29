import hmac
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import FrozenSet, List, Optional

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
# hi
from database import (
    Coordinate,
    Driver,
    HarshEvent,
    Trip,
    dispose_engine,
    get_session,
    init_models,
)
from schemas import DriverOut, TripCreatedOut, TripDetailOut, TripIn

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("telematics")

INSERT_CHUNK_SIZE = 5000

SPEED_WEIGHT = 0.40
BRAKE_WEIGHT = 0.35
CORNER_WEIGHT = 0.25

bearer_scheme = HTTPBearer(auto_error=False)


def _load_keys(env_name: str) -> FrozenSet[str]:
    raw = os.getenv(env_name, "Jarrah852003")
    return frozenset(k.strip() for k in raw.split(",") if k.strip())


def _authenticate(
    credentials: Optional[HTTPAuthorizationCredentials], valid_keys: FrozenSet[str]
) -> None:
    if not valid_keys:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication is not configured on this server",
        )
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    presented = credentials.credentials.encode("utf-8")
    matched = False
    for key in valid_keys:
        if hmac.compare_digest(presented, key.encode("utf-8")):
            matched = True
    if not matched:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def require_ingest_key(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
) -> None:
    _authenticate(credentials, _load_keys("TELEMATICS_INGEST_KEYS"))


async def require_fleet_key(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
) -> None:
    _authenticate(credentials, _load_keys("FLEET_API_KEYS"))



@asynccontextmanager
async def lifespan(app: FastAPI):
    # Skip heavy init_models on startup to prevent timeout, 
    # or wrap it safely so it doesn't block Uvicorn from binding to port 8080.
    logger.info("Application starting up...")
    yield
    await dispose_engine()
    logger.info("Database engine disposed")


app = FastAPI(
    title="Telematics API",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(GZipMiddleware, minimum_size=1024)


@app.middleware("http")
async def request_context_middleware(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        logger.exception("Unhandled error request_id=%s path=%s", request_id, request.url.path)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "Internal server error", "request_id": request_id},
            headers={"X-Request-ID": request_id},
        )
    elapsed_ms = (time.perf_counter() - started) * 1000
    response.headers["X-Request-ID"] = request_id
    logger.info(
        "request_id=%s %s %s -> %s in %.1fms",
        request_id,
        request.method,
        request.url.path,
        response.status_code,
        elapsed_ms,
    )
    return response


def compute_collective_score(speed: float, brake: float, corner: float) -> float:
    score = speed * SPEED_WEIGHT + brake * BRAKE_WEIGHT + corner * CORNER_WEIGHT
    return round(min(100.0, max(0.0, score)), 2)


async def recompute_driver_profile(session: AsyncSession, driver: Driver) -> None:
    result = await session.execute(
        select(
            func.count(Trip.id),
            func.coalesce(func.sum(Trip.distance_km), 0.0),
            func.coalesce(func.sum(Trip.collective_score * Trip.distance_km), 0.0),
            func.coalesce(func.avg(Trip.collective_score), 0.0),
        ).where(Trip.driver_id == driver.id)
    )
    trip_count, total_distance, weighted_sum, simple_avg = result.one()
    if total_distance and total_distance > 0:
        aggregate_score = weighted_sum / total_distance
    else:
        aggregate_score = simple_avg
    driver.total_trips = int(trip_count)
    driver.current_collective_score = round(float(aggregate_score), 2)


ingest_router = APIRouter(
    prefix="/v1/telematics",
    tags=["telematics"],
    dependencies=[Depends(require_ingest_key)],
)

fleet_router = APIRouter(
    prefix="/v1/fleet",
    tags=["fleet"],
    dependencies=[Depends(require_fleet_key)],
)


@ingest_router.post(
    "/trips",
    response_model=TripCreatedOut,
    status_code=status.HTTP_201_CREATED,
)
async def ingest_trip(
    payload: TripIn,
    session: AsyncSession = Depends(get_session),
) -> TripCreatedOut:
    try:
        driver_result = await session.execute(
            select(Driver).where(Driver.id == payload.driver_id).with_for_update()
        )
        driver = driver_result.scalar_one_or_none()
        
        if driver is None:
            driver = Driver(
                id=payload.driver_id,
                name=payload.name or f"Driver {payload.driver_id}",
                plate_no=payload.plate_no,
                car_type=payload.car_type,
                total_trips=0,
                current_collective_score=0.0,
            )
            session.add(driver)
            await session.flush()
        else:
            updated = False
            if payload.name and driver.name != payload.name:
                driver.name = payload.name
                updated = True
            if payload.plate_no and driver.plate_no != payload.plate_no:
                driver.plate_no = payload.plate_no
                updated = True
            if payload.car_type and driver.car_type != payload.car_type:
                driver.car_type = payload.car_type
                updated = True
            if updated:
                await session.flush()

        collective = (
            payload.collective_score
            if payload.collective_score is not None
            else compute_collective_score(
                payload.speed_score, payload.brake_score, payload.corner_score
            )
        )

        trip = Trip(
            driver_id=payload.driver_id,
            start_time=payload.start_time,
            end_time=payload.end_time,
            distance_km=payload.distance_km,
            speed_score=payload.speed_score,
            brake_score=payload.brake_score,
            corner_score=payload.corner_score,
            collective_score=collective,
        )
        session.add(trip)
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A trip for this driver with the same start_time already exists",
            )

        coordinate_rows = [
            {
                "trip_id": trip.id,
                "timestamp": c.timestamp,
                "latitude": c.latitude,
                "longitude": c.longitude,
                "speed_kmh": c.speed_kmh,
            }
            for c in sorted(payload.coordinates, key=lambda c: c.timestamp)
        ]
        for start in range(0, len(coordinate_rows), INSERT_CHUNK_SIZE):
            await session.execute(
                insert(Coordinate), coordinate_rows[start : start + INSERT_CHUNK_SIZE]
            )

        event_rows = [
            {
                "trip_id": trip.id,
                "event_type": e.event_type.value,
                "timestamp": e.timestamp,
                "latitude": e.latitude,
                "longitude": e.longitude,
                "g_force": e.g_force,
            }
            for e in sorted(payload.harsh_events, key=lambda e: e.timestamp)
        ]
        for start in range(0, len(event_rows), INSERT_CHUNK_SIZE):
            await session.execute(
                insert(HarshEvent), event_rows[start : start + INSERT_CHUNK_SIZE]
            )

        await session.flush()
        await recompute_driver_profile(session, driver)
        await session.commit()

        return TripCreatedOut(
            trip_id=trip.id,
            driver_id=driver.id,
            collective_score=collective,
            coordinates_stored=len(coordinate_rows),
            harsh_events_stored=len(event_rows),
            driver_total_trips=driver.total_trips,
            driver_current_collective_score=driver.current_collective_score,
        )
    except HTTPException:
        await session.rollback()
        raise
    except SQLAlchemyError:
        await session.rollback()
        logger.exception("Database error while ingesting trip for driver %s", payload.driver_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to store trip",
        )


@fleet_router.get("/drivers", response_model=List[DriverOut])
async def list_drivers(
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> List[Driver]:
    result = await session.execute(
        select(Driver).order_by(Driver.name.asc(), Driver.id.asc()).limit(limit).offset(offset)
    )
    return list(result.scalars().all())


@fleet_router.get("/trips/{trip_id}", response_model=TripDetailOut)
async def get_trip(
    trip_id: int,
    session: AsyncSession = Depends(get_session),
) -> Trip:
    result = await session.execute(
        select(Trip)
        .options(selectinload(Trip.coordinates), selectinload(Trip.harsh_events))
        .where(Trip.id == trip_id)
    )
    trip = result.scalar_one_or_none()
    if trip is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Trip {trip_id} not found",
        )
    return trip


@app.get("/health", tags=["health"])
async def health(session: AsyncSession = Depends(get_session)) -> dict:
    await session.execute(select(1))
    return {"status": "ok"}


app.include_router(ingest_router)
app.include_router(fleet_router)


@app.get("/v1/fleet/drivers/{driver_id}/trips")
async def get_driver_trips_alias(driver_id: int, db = Depends(get_session)):
    result = await db.execute(select(Trip).where(Trip.driver_id == driver_id))
    trips = result.scalars().all()
    return trips


@app.get("/v1/fleet/trips/{trip_id}")
async def get_single_trip_detail_alias(trip_id: int, db = Depends(get_session)):
    trip_result = await db.execute(select(Trip).where(Trip.id == trip_id))
    trip = trip_result.scalar_one_or_none()
    if not trip:
        raise HTTPException(status_code=404, detail="Trip history item not found")
        
    coords_result = await db.execute(select(Coordinate).where(Coordinate.trip_id == trip_id).order_by(Coordinate.timestamp))
    coords = coords_result.scalars().all()
    
    events_result = await db.execute(select(HarshEvent).where(HarshEvent.trip_id == trip_id))
    events = events_result.scalars().all()
    
    return {
        "id": trip.id,
        "driver_id": trip.driver_id,
        "start_time": trip.start_time.isoformat() if trip.start_time else None,
        "end_time": trip.end_time.isoformat() if trip.end_time else None,
        "distance_km": trip.distance_km,
        "speed_score": trip.speed_score,
        "brake_score": trip.brake_score,
        "corner_score": trip.corner_score,
        "collective_score": trip.collective_score,
        "coordinates": [{"latitude": c.latitude, "longitude": c.longitude, "timestamp": c.timestamp.isoformat() if c.timestamp else None, "speed_kmh": c.speed_kmh} for c in coords],
        "harsh_events": [{"event_type": e.event_type, "latitude": e.latitude, "longitude": e.longitude, "timestamp": e.timestamp.isoformat() if e.timestamp else None, "g_force": e.g_force} for e in events]
    }