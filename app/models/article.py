import uuid
from datetime import datetime, timezone
from sqlalchemy import JSON, DateTime, ForeignKey, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base


class Article(Base):
    __tablename__ = "editorial_articles"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(180), unique=True, index=True)
    excerpt: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String(60), default="Développement du cabinet")
    kind: Mapped[str] = mapped_column(String(10), default="article")
    status: Mapped[str] = mapped_column(String(12), default="draft", index=True)
    author_name: Mapped[str] = mapped_column(String(120), default="FicheAssur")
    cover_url: Mapped[str] = mapped_column(String(1000), default="")
    cover_alt: Mapped[str] = mapped_column(String(200), default="")
    seo_title: Mapped[str] = mapped_column(String(200), default="")
    seo_description: Mapped[str] = mapped_column(String(300), default="")
    blocks: Mapped[list] = mapped_column(JSON, default=list)
    editor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("brokers.id"))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
