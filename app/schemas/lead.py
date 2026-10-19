import re
from enum import Enum
from typing import Any

import phonenumbers
from pydantic import BaseModel, EmailStr, Field, field_validator

from app.config import settings


class InsuranceType(str, Enum):
    """The markets FicheAssur sells leads in (frontend src/lib/leadTypes.ts)."""
    AUTO = "AUTO"
    MOTO = "MOTO"
    AUTO_PRO = "AUTO_PRO"  # pros de l'automobile: garages, négociants, convoyeurs
    COMMERCIAL_AUTO = "COMMERCIAL_AUTO"  # véhicules pro: taxis, VTC, livreurs, flottes


# Throwaway-inbox providers. A short built-in list; swap for a maintained
# feed (or a validation API) before relying on it at scale.
DISPOSABLE_EMAIL_DOMAINS = {
    "mailinator.com", "tempmail.com", "temp-mail.org", "10minutemail.com",
    "guerrillamail.com", "yopmail.com", "yopmail.fr", "trashmail.com",
    "throwawaymail.com", "getnada.com", "sharklasers.com", "dispostable.com",
    "maildrop.cc", "fakeinbox.com", "mailnesia.com", "jetable.org",
}

# Postal code shape per target region (TARGET_REGION in .env).
ZIP_PATTERNS = {
    "FR": re.compile(r"^(?:0[1-9]|[1-8]\d|9[0-8])\d{3}$"),
    "US": re.compile(r"^\d{5}(?:-\d{4})?$"),
}


class LeadSubmit(BaseModel):
    insurance_type: InsuranceType
    zip_code: str = Field(min_length=3, max_length=10)
    city: str = Field(min_length=1, max_length=120)
    first_name: str = Field(min_length=1, max_length=80)
    last_name: str = Field(min_length=1, max_length=80)
    email: EmailStr
    phone: str = Field(min_length=6, max_length=25)
    details: dict[str, Any] = Field(default_factory=dict)
    opt_in_marketing: bool

    @field_validator("zip_code", "city", "first_name", "last_name", mode="before")
    @classmethod
    def strip(cls, v: Any) -> Any:
        return v.strip() if isinstance(v, str) else v

    @field_validator("zip_code")
    @classmethod
    def valid_zip(cls, v: str) -> str:
        pattern = ZIP_PATTERNS.get(settings.target_region.upper())
        if pattern and not pattern.match(v):
            raise ValueError("Code postal invalide.")
        return v

    @field_validator("email")
    @classmethod
    def no_disposable(cls, v: str) -> str:
        v = v.strip().lower()
        if v.rsplit("@", 1)[-1] in DISPOSABLE_EMAIL_DOMAINS:
            raise ValueError("Les adresses email temporaires ne sont pas acceptées.")
        return v

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

    @field_validator("details")
    @classmethod
    def bounded_details(cls, v: dict[str, Any]) -> dict[str, Any]:
        # Free-form by design, but not an open dumping ground.
        if len(v) > 30 or len(str(v)) > 4000:
            raise ValueError("Détails trop volumineux.")
        return v


class LeadSubmitResponse(BaseModel):
    # Deliberately identical for new and duplicate submissions, so the
    # endpoint can't be used to probe which emails/phones already exist.
    status: str = "received"
    message: str = "Votre demande a bien été enregistrée."
