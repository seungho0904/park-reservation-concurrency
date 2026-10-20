from datetime import date, datetime, time
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

PositiveId = Annotated[int, Field(strict=True, gt=0, le=9223372036854775807)]


class UserRegister(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: Annotated[str, Field(strict=True, pattern=r"^[a-z][a-z0-9_]{2,31}$")]
    display_name: Annotated[str, Field(strict=True, min_length=1, max_length=100)]
    password: Annotated[SecretStr, Field(min_length=15, max_length=128)]

    @field_validator("username", "display_name", mode="before")
    @classmethod
    def normalize_names(cls, value, info):
        if isinstance(value, str):
            value = value.strip()
            if info.field_name == "username":
                value = value.lower()
        return value


class UserOut(BaseModel):
    id: int
    username: str
    display_name: str


class ReservationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: PositiveId
    slot_id: PositiveId
    # Includes the driver and everyone aged two or older.
    passenger_count: Annotated[int, Field(strict=True, ge=1, le=8)]
    # Under two years old; no business upper limit. PostgreSQL integer storage applies.
    infant_count: Annotated[int, Field(strict=True, ge=0, le=2147483647)] = 0


class SlotOut(BaseModel):
    id: int
    park_id: int
    visit_date: date
    starts_at: time
    ends_at: time
    vehicle_capacity: int
    remaining_capacity: int


class ReservationOut(BaseModel):
    id: int
    user_id: int
    slot_id: int
    park_id: int
    park_name: str
    park_timezone: str
    visit_date: date
    starts_at: time
    ends_at: time
    passenger_count: int
    infant_count: int
    created_at: datetime
