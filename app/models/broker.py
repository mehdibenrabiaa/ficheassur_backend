import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, Enum, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class BrokerRole(str, enum.Enum):
    broker = "broker"
    admin = "admin"  # runs FicheAssur: accepts brokers, (later) refunds and prices


class BrokerStatus(str, enum.Enum):
    invited = "invited"    # accepted, hasn't set a password yet
    active = "active"
    disabled = "disabled"  # can't sign in; nothing is deleted


class Broker(Base):
    """An espace pro account. Usually self-service (POST /auth/register,
    active at once); an admin can also open one from an AccessRequest, the
    broker then setting a password through a one-time activation link."""

    __tablename__ = "brokers"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(120))
    company: Mapped[str] = mapped_column(String(160))
    phone: Mapped[str | None] = mapped_column(String(20))
    orias_number: Mapped[str | None] = mapped_column(String(8))
    lead_types: Mapped[list[str]] = mapped_column(JSON, default=list)
    role: Mapped[BrokerRole] = mapped_column(Enum(BrokerRole), default=BrokerRole.broker)
    status: Mapped[BrokerStatus] = mapped_column(Enum(BrokerStatus), default=BrokerStatus.invited)
    password_hash: Mapped[str | None] = mapped_column(String(255))
    # Only the hash of the activation token is stored; the link holds the token.
    activation_token_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    activation_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    access_request_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
