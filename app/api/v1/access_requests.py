from fastapi import APIRouter, Depends, Request
from slowapi.util import get_remote_address
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.leads import limiter
from app.database import get_db
from app.models.access_request import AccessRequest
from app.schemas.access_request import AccessRequestCreate, AccessRequestResponse

router = APIRouter(prefix="/api/v1/access-requests", tags=["access-requests"])


@router.post("", response_model=AccessRequestResponse, status_code=201)
@limiter.limit("5/hour")
async def create_access_request(
    request: Request,
    payload: AccessRequestCreate,
    db: AsyncSession = Depends(get_db),
):
    data = payload.model_dump()
    data["lead_types"] = [t.value for t in payload.lead_types]
    db.add(AccessRequest(**data, ip_address=get_remote_address(request)))
    await db.commit()
    return AccessRequestResponse()
