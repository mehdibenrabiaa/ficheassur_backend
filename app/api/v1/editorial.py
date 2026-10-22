"""Admin-authored, structured editorial content; public access to published items only."""
import uuid
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import urlsplit
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.models.article import Article
from app.models.broker import Broker
from app.security import current_admin

admin_router = APIRouter(prefix="/api/v1/admin/articles", tags=["editorial admin"], dependencies=[Depends(current_admin)])
public_router = APIRouter(prefix="/api/v1/articles", tags=["articles"])


class Block(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    type: Literal["paragraph", "heading", "list"]
    text: str = Field(min_length=1, max_length=12000)


class ArticleIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(min_length=3, max_length=200)
    slug: str = Field(min_length=3, max_length=180, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    excerpt: str = Field(default="", max_length=1500)
    category: str = Field(default="Développement du cabinet", min_length=1, max_length=60)
    kind: Literal["article", "guide"] = "article"
    status: Literal["draft", "published"] = "draft"
    author_name: str = Field(default="FicheAssur", min_length=1, max_length=120)
    cover_url: str = Field(default="", max_length=1000)
    cover_alt: str = Field(default="", max_length=200)
    seo_title: str = Field(default="", max_length=200)
    seo_description: str = Field(default="", max_length=300)
    blocks: list[Block] = Field(default_factory=list, max_length=200)

    @field_validator("cover_url")
    @classmethod
    def safe_image(cls, value):
        if not value:
            return value
        url = urlsplit(value)
        if (value.startswith("/") and not value.startswith("//") and "\\" not in value) or (url.scheme == "https" and url.hostname and not url.username):
            return value
        raise ValueError("Image : utilisez un chemin du site ou une URL HTTPS.")

    @model_validator(mode="after")
    def ready_for_publication(self):
        if self.status == "published" and (not self.excerpt or not self.blocks):
            raise ValueError("Une introduction et du contenu sont requis pour publier.")
        return self


def out(a, *, content=True):
    data = {key: getattr(a, key) for key in ("title", "slug", "excerpt", "category", "kind", "status", "author_name", "cover_url", "cover_alt", "seo_title", "seo_description", "published_at", "created_at", "updated_at")}
    data["id"] = str(a.id)
    if content:
        data["blocks"] = a.blocks
    data["reading_minutes"] = max(1, round(sum(len(b.get("text", "").split()) for b in a.blocks) / 200))
    return data


async def listing(db, q, page, limit, content):
    total = await db.scalar(select(func.count()).select_from(q.subquery()))
    items = (await db.execute(q.order_by(Article.published_at.desc(), Article.updated_at.desc(), Article.id).offset((page - 1) * limit).limit(limit))).scalars().all()
    return {"items": [out(a, content=content) for a in items], "total": total, "page": page, "limit": limit}


@admin_router.get("")
async def list_admin(q: str = Query(default="", max_length=200), status: Literal["draft", "published"] | None = None, page: int = Query(1, ge=1), limit: int = Query(20, ge=1, le=100), db: AsyncSession = Depends(get_db)):
    query = select(Article)
    if q:
        query = query.where(Article.title.ilike(f"%{q}%"))
    if status:
        query = query.where(Article.status == status)
    return await listing(db, query, page, limit, False)


@admin_router.get("/{article_id}")
async def get_admin(article_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    article = await db.get(Article, article_id)
    if not article:
        raise HTTPException(404, "Article introuvable.")
    return out(article)


async def save(db, article, body, editor):
    # A published address stays stable. Unpublish before changing its slug.
    if article.id and article.status == "published" and article.slug != body.slug:
        raise HTTPException(409, "Dépubliez l’article avant de modifier son adresse.")
    for key, value in body.model_dump().items():
        setattr(article, key, value)
    article.editor_id = editor.id
    if body.status == "published" and not article.published_at:
        article.published_at = datetime.now(timezone.utc)
    article.updated_at = datetime.now(timezone.utc)
    db.add(article)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "Cette adresse est déjà utilisée par un article.")
    await db.refresh(article)
    return out(article)


@admin_router.post("", status_code=201)
async def create(body: ArticleIn, editor: Broker = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    return await save(db, Article(), body, editor)


@admin_router.put("/{article_id}")
async def update(article_id: uuid.UUID, body: ArticleIn, editor: Broker = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    article = await db.get(Article, article_id)
    if not article:
        raise HTTPException(404, "Article introuvable.")
    return await save(db, article, body, editor)


@public_router.get("")
async def list_public(q: str = Query(default="", max_length=200), category: str | None = Query(None, max_length=60), kind: Literal["article", "guide"] | None = None, page: int = Query(1, ge=1), limit: int = Query(12, ge=1, le=100), db: AsyncSession = Depends(get_db)):
    query = select(Article).where(Article.status == "published")
    if q:
        query = query.where(Article.title.ilike(f"%{q}%") | Article.excerpt.ilike(f"%{q}%"))
    if category:
        query = query.where(Article.category == category)
    if kind:
        query = query.where(Article.kind == kind)
    return await listing(db, query, page, limit, False)


@public_router.get("/{slug}")
async def get_public(slug: str, db: AsyncSession = Depends(get_db)):
    article = await db.scalar(select(Article).where(Article.slug == slug, Article.status == "published"))
    if not article:
        raise HTTPException(404, "Article introuvable.")
    return out(article)
