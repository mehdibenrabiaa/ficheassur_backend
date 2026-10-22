import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class OAuthIdentity(Base):
    """A sign-in provider account linked to a broker ("Continuer avec
    Microsoft"). Matched on the provider's stable user id, never on the email
    alone: a Microsoft account can carry an email its owner never proved, so
    an email match must not open someone else's FicheAssur account."""

    __tablename__ = "oauth_identities"
    __table_args__ = (UniqueConstraint("provider", "subject", name="uq_oauth_identity"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    provider: Mapped[str] = mapped_column(String(20))  # "microsoft"
    subject: Mapped[str] = mapped_column(String(255))  # the provider's id for this user (tenant + object id)
    broker_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("brokers.id"), index=True)
    email: Mapped[str | None] = mapped_column(String(254))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
