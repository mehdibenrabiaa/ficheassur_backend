import re
from typing import Any

import phonenumbers
from pydantic import BaseModel, EmailStr, Field, field_validator

from app.config import settings
from app.schemas.lead import InsuranceType


class AccessRequestCreate(BaseModel):
    full_name: str = Field(min_length=2, max_length=120)
    company: str = Field(min_length=2, max_length=160)
    email: EmailStr
    phone: str = Field(min_length=6, max_length=25)
    # Brokers are ORIAS-registered: an 8-digit number. Optional on the form,
    # checked by hand before an account is opened.
    orias_number: str | None = Field(default=None, max_length=12)
    lead_types: list[InsuranceType] = Field(min_length=1)
    message: str | None = Field(default=None, max_length=2000)

    @field_validator("full_name", "company", "message", "orias_number", mode="before")
    @classmethod
    def strip(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = v.strip()
            return v or None
        return v

    @field_validator("email")
    @classmethod
    def lower(cls, v: str) -> str:
        return v.strip().lower()

    @field_validator("orias_number")
    @classmethod
    def orias(cls, v: str | None) -> str | None:
        if v is None:
            return None
        digits = re.sub(r"\D", "", v)
        if len(digits) != 8:
            raise ValueError("Le numéro ORIAS comporte 8 chiffres.")
        return digits

    @field_validator("phone")
    @classmethod
    def e164(cls, v: str) -> str:
        try:
            num = phonenumbers.parse(v, settings.target_region.upper())
        except phonenumbers.NumberParseException:
            raise ValueError("Numéro de téléphone invalide.")
        if not phonenumbers.is_valid_number(num):
            raise ValueError("Numéro de téléphone invalide.")
        return phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.E164)


class AccessRequestResponse(BaseModel):
    status: str = "received"
