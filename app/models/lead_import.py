import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class LeadImport(Base):
    """Where an imported lead came from: New World Courtage puts a lead on
    sale from its CRM, and it arrives here through /api/v1/imports/leads.
    (source, source_ref) is unique, so sending the same lead twice updates it
    instead of creating a duplicate. Its own table: no change to `leads`."""

    __tablename__ = "lead_imports"
    __table_args__ = (UniqueConstraint("source", "source_ref", name="uq_lead_import_source"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    source: Mapped[str] = mapped_column(String(40))  # "nwc"
    source_ref: Mapped[str] = mapped_column(String(80))  # the lead's id over there
    lead_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("leads.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
