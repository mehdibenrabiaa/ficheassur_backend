import logging
import uuid

logger = logging.getLogger("ficheassur.matching")


async def match_lead(lead_id: uuid.UUID) -> None:
    """Hand a new lead to buyers. Not built yet: there's no buyer/partner
    model to match against, so leads stay NEW until this exists. Runs as a
    background task, after the response has gone out."""
    logger.info("Lead %s queued for matching (no matcher configured yet)", lead_id)
