"""Strict, allowlisted transport contract for the existing bot repository methods."""

import re
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr


class Call(BaseModel):
    model_config = ConfigDict(extra="forbid")

    args: list[StrictInt | StrictStr] = Field(max_length=3)


# The API accepts only these methods and exact argument types. Never expose SQL or
# arbitrary Python method names through the transport.
ARG_TYPES: dict[str, tuple[type, ...]] = {
    "health": (),
    "claim_update": (int,),
    "complete_update": (int, str),
    "release_update": (int, str),
    "get_user": (int,),
    "select_section": (int, str),
    "has_section": (int, str),
    "list_services": (),
    "get_service": (int,),
    "list_slots": (int,),
    "create_booking": (int, int, int),
    "get_booking": (int,),
    "decide_booking": (int, str),
    "claim_notification": (int,),
    "complete_notification": (int, int, int),
    "retry_notification": (int, int),
    "list_bookings": (int,),
    "list_products": (),
    "get_product": (int,),
    "list_webinars": (),
    "get_webinar": (int,),
    "resolve_recording": (str,),
    "get_recording_context": (int,),
    "list_owned_recordings": (int,),
    "list_entitlements": (int,),
}


def valid_args(operation: str, args: list[int | str]) -> bool:
    expected = ARG_TYPES.get(operation)
    if expected is None or len(args) != len(expected):
        return False
    if any(type(value) is not kind for value, kind in zip(args, expected, strict=True)):
        return False
    if any(not (-(2**63) <= value < 2**63) for value in args if type(value) is int):
        return False
    if operation == "resolve_recording" and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", args[0]):
        return False
    if operation in {"get_recording_context", "list_owned_recordings"} and args[0] <= 0:
        return False
    if operation in {"select_section", "has_section"} and args[1] not in {"pro", "client"}:
        return False
    if operation in {"complete_update", "release_update"}:
        if len(args[1]) > 40:
            return False
        try:
            lease_time = datetime.fromisoformat(args[1])
        except ValueError:
            return False
        if lease_time.tzinfo is None:
            return False
    return not (operation == "decide_booking" and args[1] not in {"confirmed", "declined"})
