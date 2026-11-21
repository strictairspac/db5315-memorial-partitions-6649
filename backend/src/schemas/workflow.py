from datetime import date
from typing import Any

from pydantic import BaseModel, Field


class LoginForm(BaseModel):
    username: str = Field(min_length=1, max_length=40)
    password: str = Field(min_length=1, max_length=128)


class CaseForm(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    payload: dict[str, Any]


class ReviewForm(BaseModel):
    note: str = Field(min_length=1, max_length=500)


class IngestRow(BaseModel):
    slip_id: str = Field(min_length=1, max_length=64)
    entered_on: date


class IngestForm(BaseModel):
    batch_id: str = Field(min_length=1, max_length=64)
    gate: str = Field(min_length=1, max_length=40)
    rows: list[IngestRow] = Field(min_length=1, max_length=500)


class CorrectionForm(BaseModel):
    order_id: str = Field(min_length=1, max_length=64)
    slip_id: str = Field(min_length=1, max_length=64)
    corrected_on: date


class MonthForm(BaseModel):
    month: date
