"""Strict transport models for an authenticated, unpaid order intent."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt


class OrderIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    telegram_id: StrictInt = Field(ge=-(2**63), le=2**63 - 1)
    webinar_id: StrictInt = Field(ge=1, le=2**63 - 1)
    source_update_id: StrictInt = Field(ge=1, le=2**63 - 1)


class PendingOrder(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: StrictInt = Field(ge=1)
    webinar_id: StrictInt = Field(ge=1)
    amount_kopeks: StrictInt = Field(gt=0)
    currency: Literal["RUB"]
    status: Literal["pending", "paid", "cancelled", "refunded"]
