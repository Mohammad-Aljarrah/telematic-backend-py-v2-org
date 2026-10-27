from datetime import datetime
from enum import Enum
from typing import List, Optional

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)


class HarshEventType(str, Enum):
    HARSH_BRAKING = "harsh_braking"
    HARSH_ACCELERATION = "harsh_acceleration"
    HARSH_CORNERING = "harsh_cornering"


# ---------------------------------------------------------------------------
# Incoming mobile payload
# ---------------------------------------------------------------------------

class CoordinateIn(BaseModel):
    timestamp: AwareDatetime
    latitude: float = Field(..., ge=-90.0, le=90.0)
    longitude: float = Field(..., ge=-180.0, le=180.0)
    speed_kmh: float = Field(..., ge=0.0, le=400.0)


class HarshEventIn(BaseModel):
    event_type: HarshEventType
    timestamp: AwareDatetime
    latitude: float = Field(..., ge=-90.0, le=90.0)
    longitude: float = Field(..., ge=-180.0, le=180.0)
    g_force: float = Field(..., ge=0.0, le=20.0)


from pydantic import BaseModel, Field, AwareDatetime, model_validator, ConfigDict
from typing import Optional, List
from datetime import datetime

class TripIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    driver_id: int = Field(..., gt=0)
    name: Optional[str] = Field(default=None, alias="driver_name", description="Driver Name")
    plate_no: Optional[str] = Field(default=None, alias="plate_number", description="Vehicle Plate Number")
    car_type: Optional[str] = Field(default=None, description="Vehicle Car Type")
    start_time: AwareDatetime
    end_time: AwareDatetime
    distance_km: float = Field(..., ge=0.0, le=20000.0)
    speed_score: float = Field(..., ge=0.0, le=100.0)
    brake_score: float = Field(..., ge=0.0, le=100.0)
    corner_score: float = Field(..., ge=0.0, le=100.0)
    collective_score: Optional[float] = Field(default=None, ge=0.0, le=100.0)
    coordinates: List[CoordinateIn] = Field(..., min_length=1, max_length=100000)
    harsh_events: List[HarshEventIn] = Field(default_factory=list, max_length=10000)

    @model_validator(mode="after")
    def validate_time_window(self) -> "TripIn":
        if self.end_time <= self.start_time:
            raise ValueError("end_time must be later than start_time")
        return self


class TripSummaryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    driver_id: int
    start_time: datetime
    end_time: datetime
    distance_km: float
    speed_score: float
    brake_score: float
    corner_score: float
    collective_score: float


# ---------------------------------------------------------------------------
# Outgoing responses
# ---------------------------------------------------------------------------

class CoordinateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    timestamp: datetime
    latitude: float
    longitude: float
    speed_kmh: float


class HarshEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    event_type: str
    timestamp: datetime
    latitude: float
    longitude: float
    g_force: float


class DriverOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    plate_no: Optional[str] = None
    car_type: Optional[str] = None
    registered_at: datetime
    total_trips: int
    current_collective_score: float


class DriverSummaryOut(DriverOut):
    total_distance_km: float


class TripDetailOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    driver_id: int
    start_time: datetime
    end_time: datetime
    distance_km: float
    speed_score: float
    brake_score: float
    corner_score: float
    collective_score: float
    coordinates: List[CoordinateOut]
    harsh_events: List[HarshEventOut]


class TripCreatedOut(BaseModel):
    trip_id: int
    driver_id: int
    collective_score: float
    coordinates_stored: int
    harsh_events_stored: int
    driver_total_trips: int
    driver_current_collective_score: float