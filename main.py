import hashlib
import hmac
import logging
import os
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, FrozenSet, List, Optional

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import func, insert, or_, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, selectinload

from database import (
    Coordinate,
    Device,
    Driver,
    HarshEvent,
    Trip,
    Vehicle,
    dispose_engine,
    get_session,
)
from schemas import (
    DriverCreate,
    DriverOut,
    DriverRestoreIn,
    RecoveryCodeOut,
    TripCreatedOut,
    TripDetailOut,
    TripIn,
    TripSummaryOut,
)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("telematics")

INSERT_CHUNK_SIZE = 5000


SPEED_WEIGHT = 0.40
BRAKE_WEIGHT = 0.35
CORNER_WEIGHT = 0.25

# Older app builds do not send driver_uid. While true, such uploads are attached to the
# oldest driver with the same name (and never overwrite that driver's details).
# Set ALLOW_LEGACY_DRIVER_LOOKUP=false once every phone runs the new app.
ALLOW_LEGACY_DRIVER_LOOKUP = os.getenv("ALLOW_LEGACY_DRIVER_LOOKUP", "true").lower() == "true"

bearer_scheme = HTTPBearer(auto_error=False)


# --------------------------------------------------------------------------- #
# Authentication
# --------------------------------------------------------------------------- #
def _load_keys(env_name: str) -> FrozenSet[str]:
    # No default key: if the variable is unset the endpoints answer 503.
    raw = os.getenv(env_name, "")
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


# --------------------------------------------------------------------------- #
# App setup
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Application starting up...")
    yield
    await dispose_engine()
    logger.info("Database engine disposed")


def _cors_origins() -> List[str]:
    raw = os.getenv("CORS_ORIGINS", "*")
    origins = [o.strip() for o in raw.split(",") if o.strip()]
    return origins or ["*"]


app = FastAPI(
    title="Telematics API",
    version="3.0.0",
    lifespan=lifespan,
)

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


# Middleware order matters: the one added LAST is the outermost. CORS is added last on purpose so
# that every answer, including a 500 error, carries the CORS headers. Otherwise the browser hides
# the real error and the dashboard only says it cannot reach the API.
# Auth uses a Bearer header (no cookies), so credentials are not needed.
# Set CORS_ORIGINS=https://your-dashboard.vercel.app to restrict browser access.
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def compute_collective_score(speed: float, brake: float, corner: float) -> float:
    score = speed * SPEED_WEIGHT + brake * BRAKE_WEIGHT + corner * CORNER_WEIGHT
    return round(min(100.0, max(0.0, score)), 2)


def _clean(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _normalize_plate(value: Optional[str]) -> Optional[str]:
    value = _clean(value)
    return value.upper() if value else None


# One-time recovery codes: a fleet manager issues one for a driver, the driver types it into the
# app to restore their profile on a new phone or to reset a forgotten password.
# Set RECOVERY_CODE_PEPPER to a long random secret so stored hashes cannot be guessed offline.
RECOVERY_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"  # no 0/O, 1/I/L
RECOVERY_CODE_LENGTH = 8
RECOVERY_CODE_TTL = timedelta(hours=24)
RECOVERY_MAX_FAILURES = 5


def _generate_recovery_code() -> str:
    return "".join(secrets.choice(RECOVERY_ALPHABET) for _ in range(RECOVERY_CODE_LENGTH))


def _normalize_recovery_code(raw: str) -> str:
    return "".join(ch for ch in raw.upper() if ch.isalnum())


def _hash_recovery_code(code: str) -> str:
    pepper = os.getenv("RECOVERY_CODE_PEPPER", "")
    return hashlib.sha256((pepper + code).encode("utf-8")).hexdigest()


def _clear_recovery_code(driver: Driver) -> None:
    driver.recovery_code_hash = None
    driver.recovery_expires_at = None
    driver.recovery_failures = 0


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def driver_to_dict(
    driver: Driver,
    device_uid: Optional[str],
    total_distance_km: Optional[float] = None,
) -> Dict[str, Any]:
    return {
        "id": driver.id,
        "driver_uid": driver.driver_uid,
        "name": driver.name,
        "plate_no": driver.plate_no,
        "car_type": driver.car_type,
        "device_uid": device_uid,
        "registered_at": driver.registered_at,
        "total_trips": driver.total_trips,
        "current_collective_score": driver.current_collective_score,
        "total_distance_km": total_distance_km,
    }


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


async def _get_or_create_device(
    session: AsyncSession, device_uid: str, model: Optional[str]
) -> Device:
    device_uid = device_uid.strip()
    result = await session.execute(select(Device).where(Device.device_uid == device_uid))
    device = result.scalar_one_or_none()
    if device is not None:
        if model and not device.model:
            device.model = model.strip()
        return device
    try:
        async with session.begin_nested():
            device = Device(device_uid=device_uid, model=_clean(model))
            session.add(device)
            await session.flush()
    except IntegrityError:
        result = await session.execute(select(Device).where(Device.device_uid == device_uid))
        device = result.scalar_one()
    return device


async def _get_or_create_vehicle(
    session: AsyncSession, plate_no: str, car_type: Optional[str]
) -> Vehicle:
    result = await session.execute(select(Vehicle).where(Vehicle.plate_no == plate_no))
    vehicle = result.scalar_one_or_none()
    if vehicle is not None:
        if car_type and not vehicle.car_type:
            vehicle.car_type = car_type.strip()
        return vehicle
    try:
        async with session.begin_nested():
            vehicle = Vehicle(plate_no=plate_no, car_type=_clean(car_type))
            session.add(vehicle)
            await session.flush()
    except IntegrityError:
        result = await session.execute(select(Vehicle).where(Vehicle.plate_no == plate_no))
        vehicle = result.scalar_one()
    return vehicle


async def _find_driver_by_uid(session: AsyncSession, driver_uid: str) -> Optional[Driver]:
    result = await session.execute(
        select(Driver).where(Driver.driver_uid == driver_uid).with_for_update()
    )
    return result.scalar_one_or_none()


async def _claim_legacy_driver(
    session: AsyncSession,
    driver_uid: str,
    name: str,
    device_id: Optional[int],
) -> Optional[Driver]:
    """Adopt a pre-migration driver row (same exact name, not yet claimed) for this app identity,
    so history recorded before multi-driver support stays attached to the right person."""
    result = await session.execute(
        select(Driver)
        .where(Driver.name == name, Driver.legacy_unclaimed.is_(True))
        .order_by(Driver.id.asc())
        .limit(1)
        .with_for_update()
    )
    legacy = result.scalar_one_or_none()
    if legacy is None:
        return None
    try:
        async with session.begin_nested():
            legacy.driver_uid = driver_uid
            legacy.legacy_unclaimed = False
            if legacy.device_id is None and device_id is not None:
                legacy.device_id = device_id
            await session.flush()
    except IntegrityError:
        # Another request registered this driver_uid first.
        return await _find_driver_by_uid(session, driver_uid)
    logger.info("Claimed legacy driver id=%s for driver_uid=%s", legacy.id, driver_uid)
    return legacy


async def _create_driver(
    session: AsyncSession,
    *,
    driver_uid: Optional[str],
    name: str,
    plate_no: Optional[str],
    car_type: Optional[str],
    device_id: Optional[int],
) -> Driver:
    # Guarantee a valid UUID string if none was provided
    uid = driver_uid.strip() if driver_uid and driver_uid.strip() else str(uuid.uuid4())
    
    driver = Driver(
        driver_uid=uid,  # <--- Explicitly supplied so SQLAlchemy includes it in INSERT
        name=name,
        plate_no=plate_no,
        car_type=car_type,
        device_id=device_id,
        total_trips=0,
        current_collective_score=0.0,
    )
    session.add(driver)
    await session.flush()
    return driver

# --------------------------------------------------------------------------- #
# Routers
# --------------------------------------------------------------------------- #
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


@ingest_router.post("/drivers", response_model=DriverOut)
async def register_driver(
    payload: DriverCreate,
    session: AsyncSession = Depends(get_session),
) -> Dict[str, Any]:
    """Driver signup from the app. Idempotent: keyed on driver_uid, never on name."""
    try:
        name = payload.name.strip()
        if not name:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="name must not be empty",
            )
        device = await _get_or_create_device(session, payload.device_uid, payload.device_model)
        driver = await _find_driver_by_uid(session, payload.driver_uid.strip())
        if driver is None:
            driver = await _claim_legacy_driver(
                session, payload.driver_uid.strip(), name, device.id
            )
        if driver is None:
            driver = await _create_driver(
                session,
                driver_uid=payload.driver_uid.strip(),
                name=name,
                plate_no=_clean(payload.plate_no),
                car_type=_clean(payload.car_type),
                device_id=device.id,
            )
        else:
            # Explicit profile update by the driver themself.
            driver.name = name
            if _clean(payload.plate_no):
                driver.plate_no = _clean(payload.plate_no)
            if _clean(payload.car_type):
                driver.car_type = _clean(payload.car_type)
            if driver.device_id is None:
                driver.device_id = device.id
            await session.flush()
        await session.refresh(driver)
        response = driver_to_dict(driver, device.device_uid)
        await session.commit()
        return response
    except HTTPException:
        await session.rollback()
        raise
    except SQLAlchemyError:
        await session.rollback()
        logger.exception("Database error while registering driver %s", payload.name)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to register driver",
        )


@ingest_router.post("/drivers/restore", response_model=DriverOut)
async def restore_driver(
    payload: DriverRestoreIn,
    session: AsyncSession = Depends(get_session),
) -> Dict[str, Any]:
    """Returns the driver a one-time recovery code was issued for, then burns the code.

    Used by the app on a new phone, after a reinstall, or to reset a forgotten password. The code
    must be issued by a fleet manager, is valid for 24 hours, and is revoked after 5 wrong tries.
    """
    try:
        name = payload.name.strip()
        presented = _hash_recovery_code(_normalize_recovery_code(payload.recovery_code))
        now = datetime.now(timezone.utc)

        result = await session.execute(
            select(Driver)
            .where(func.lower(Driver.name) == name.lower(), Driver.recovery_code_hash.is_not(None))
            .with_for_update()
        )
        candidates = list(result.scalars().all())

        matched: Optional[Driver] = None
        for candidate in candidates:
            expires = candidate.recovery_expires_at
            if expires is None or _as_utc(expires) <= now:
                _clear_recovery_code(candidate)
                continue
            if hmac.compare_digest(candidate.recovery_code_hash or "", presented):
                matched = candidate
                break

        if matched is None:
            for candidate in candidates:
                if candidate.recovery_code_hash is None:  # expired and just cleared
                    continue
                candidate.recovery_failures += 1
                if candidate.recovery_failures >= RECOVERY_MAX_FAILURES:
                    _clear_recovery_code(candidate)
            await session.commit()
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Invalid or expired recovery code",
            )

        device = await _get_or_create_device(session, payload.device_uid, payload.device_model)
        _clear_recovery_code(matched)
        matched.legacy_unclaimed = False
        if matched.device_id is None:
            matched.device_id = device.id
        await session.flush()
        response = driver_to_dict(matched, device.device_uid)
        await session.commit()
        logger.info("Recovery code redeemed for driver id=%s", matched.id)
        return response
    except HTTPException:
        await session.rollback()
        raise
    except SQLAlchemyError:
        await session.rollback()
        logger.exception("Database error while restoring a driver")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to restore driver",
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
        driver_name = payload.name.strip()
        plate_display = _clean(payload.plate_no)
        plate_key = _normalize_plate(payload.plate_no)
        car_type = _clean(payload.car_type)

        device: Optional[Device] = None
        if payload.device_uid:
            device = await _get_or_create_device(
                session, payload.device_uid, payload.device_model
            )

        vehicle: Optional[Vehicle] = None
        if plate_key:
            vehicle = await _get_or_create_vehicle(session, plate_key, car_type)

        if payload.driver_uid:
            driver_uid = payload.driver_uid.strip()
            driver = await _find_driver_by_uid(session, driver_uid)
            if driver is None:
                driver = await _claim_legacy_driver(
                    session, driver_uid, driver_name, device.id if device else None
                )
            if driver is None:
                # Driver never signed up explicitly: create it from this payload once.
                driver = await _create_driver(
                    session,
                    driver_uid=str(uuid.uuid4()),  # <--- THIS FIXES THE NOT-NULL ERROR!
                    name=driver_name,
                    plate_no=plate_display,
                    car_type=car_type,
                    device_id=device.id if device else None,
                )
            # An existing driver is never modified by a trip upload.
        else:
            if not ALLOW_LEGACY_DRIVER_LOOKUP:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="driver_uid is required",
                )
            logger.warning("Legacy trip upload without driver_uid for name=%s", driver_name)
            legacy_result = await session.execute(
                select(Driver)
                .where(Driver.name == driver_name)
                .order_by(Driver.id.asc())
                .limit(1)
                .with_for_update()
            )
            driver = legacy_result.scalar_one_or_none()
            if driver is None:
                driver = await _create_driver(
                    session,
                    driver_uid=None,
                    name=driver_name,
                    plate_no=plate_display,
                    car_type=car_type,
                    device_id=device.id if device else None,
                )

        collective = (
            payload.collective_score
            if payload.collective_score is not None
            else compute_collective_score(
                payload.speed_score, payload.brake_score, payload.corner_score
            )
        )

        trip = Trip(
            driver_id=driver.id,
            device_id=device.id if device else None,
            vehicle_id=vehicle.id if vehicle else None,
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
        logger.exception("Database error while ingesting trip for driver %s", payload.name)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to store trip",
        )


@fleet_router.get("/drivers", response_model=List[DriverOut])
async def list_drivers(
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    device_uid: Optional[str] = Query(None, max_length=64),
    plate_no: Optional[str] = Query(None, max_length=50),
    q: Optional[str] = Query(None, max_length=255, description="Name contains"),
    session: AsyncSession = Depends(get_session),
) -> List[Dict[str, Any]]:
    distance_by_driver = (
        select(
            Trip.driver_id.label("driver_id"),
            func.coalesce(func.sum(Trip.distance_km), 0.0).label("total_km"),
        )
        .group_by(Trip.driver_id)
        .subquery()
    )
    stmt = (
        select(Driver, Device.device_uid, func.coalesce(distance_by_driver.c.total_km, 0.0))
        .outerjoin(Device, Driver.device_id == Device.id)
        .outerjoin(distance_by_driver, distance_by_driver.c.driver_id == Driver.id)
    )

    if device_uid:
        trip_device = aliased(Device)
        drivers_with_trips_on_device = (
            select(Trip.driver_id)
            .join(trip_device, Trip.device_id == trip_device.id)
            .where(trip_device.device_uid == device_uid.strip())
        )
        stmt = stmt.where(
            or_(
                Device.device_uid == device_uid.strip(),
                Driver.id.in_(drivers_with_trips_on_device),
            )
        )
    if plate_no:
        stmt = stmt.where(func.upper(Driver.plate_no) == plate_no.strip().upper())
    if q:
        stmt = stmt.where(Driver.name.ilike(f"%{q.strip()}%"))

    stmt = stmt.order_by(Driver.name.asc(), Driver.id.asc()).limit(limit).offset(offset)
    result = await session.execute(stmt)
    return [
        driver_to_dict(driver, dev_uid, float(total_km))
        for driver, dev_uid, total_km in result.all()
    ]


@fleet_router.post("/drivers/{driver_id}/recovery-code", response_model=RecoveryCodeOut)
async def issue_recovery_code(
    driver_id: int,
    session: AsyncSession = Depends(get_session),
) -> RecoveryCodeOut:
    """Issues a one-time recovery code for a driver (replaces any earlier one). Shown only once."""
    try:
        result = await session.execute(
            select(Driver).where(Driver.id == driver_id).with_for_update()
        )
        driver = result.scalar_one_or_none()
        if driver is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Driver {driver_id} not found",
            )
        code = _generate_recovery_code()
        expires_at = datetime.now(timezone.utc) + RECOVERY_CODE_TTL
        driver.recovery_code_hash = _hash_recovery_code(code)
        driver.recovery_expires_at = expires_at
        driver.recovery_failures = 0
        await session.commit()
        logger.info("Recovery code issued for driver id=%s", driver.id)
        return RecoveryCodeOut(
            driver_id=driver.id,
            driver_name=driver.name,
            code=f"{code[:4]}-{code[4:]}",
            expires_at=expires_at,
        )
    except HTTPException:
        await session.rollback()
        raise
    except SQLAlchemyError:
        await session.rollback()
        logger.exception("Database error while issuing a recovery code")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to issue recovery code",
        )


@fleet_router.get("/drivers/{driver_id}/trips", response_model=List[TripSummaryOut])
async def list_driver_trips(
    driver_id: int,
    limit: int = Query(200, ge=1, le=2000),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> List[Trip]:
    exists = await session.execute(select(Driver.id).where(Driver.id == driver_id))
    if exists.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Driver {driver_id} not found",
        )
    result = await session.execute(
        select(Trip)
        .options(selectinload(Trip.device), selectinload(Trip.vehicle))
        .where(Trip.driver_id == driver_id)
        .order_by(Trip.start_time.desc(), Trip.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(result.scalars().all())


@fleet_router.get("/trips/{trip_id}", response_model=TripDetailOut)
async def get_trip(
    trip_id: int,
    session: AsyncSession = Depends(get_session),
) -> Trip:
    result = await session.execute(
        select(Trip)
        .options(
            selectinload(Trip.coordinates),
            selectinload(Trip.harsh_events),
            selectinload(Trip.device),
            selectinload(Trip.vehicle),
        )
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
async def health() -> dict:
    return {"status": "ok"}


app.include_router(ingest_router)
app.include_router(fleet_router)



from pydantic import BaseModel

class DriverRegisterIn(BaseModel):
    driver_uid: str
    driver_name: str
    car_type: Optional[str] = None
    plate_number: Optional[str] = None
    device_uid: Optional[str] = None
    device_model: Optional[str] = None

@app.post("/v1/telematics/drivers", status_code=status.HTTP_201_CREATED)
async def register_driver(
    payload: DriverRegisterIn,
    session: AsyncSession = Depends(get_session),
    authorized: bool = Depends(verify_ingest_key),  # or your auth dependency
):
    # Check if driver already exists by driver_uid
    driver = await _find_driver_by_uid(session, payload.driver_uid)
    if driver is None:
        driver = Driver(
            driver_uid=payload.driver_uid,
            name=payload.driver_name,
            plate_no=payload.plate_number,
            car_type=payload.car_type,
            total_trips=0,
            current_collective_score=0.0,
        )
        session.add(driver)
    else:
        # Update details if provided
        if payload.driver_name:
            driver.name = payload.driver_name
        if payload.plate_number:
            driver.plate_no = payload.plate_number
        if payload.car_type:
            driver.car_type = payload.car_type

    await session.commit()
    return {"status": "registered", "driver_uid": driver.driver_uid}