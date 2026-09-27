"""Simple keyword/alias resolution against small reference-data tables
(ENVIRONMENT, TEAM). Deliberately not OPA-based: these are small, fixed
catalogs (~10 rows) where a plain regex scan is simpler and sufficient,
unlike SOURCE_SYSTEM's larger, priority-tiered matching.

ENVIRONMENT is used for incident context only, never for source-system
routing. TEAM is reporting metadata only, never a classification
input.
"""

import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Environment, Team


def _matches(text: str, code: str, aliases: str | None) -> bool:
    candidates = [code] + ([a.strip() for a in aliases.split(",") if a.strip()] if aliases else [])
    return any(re.search(rf"\b{re.escape(candidate)}\b", text, re.IGNORECASE) for candidate in candidates)


async def resolve_environment(session: AsyncSession, text: str) -> Environment | None:
    environments = (await session.scalars(select(Environment).where(Environment.in_scope.is_(True)))).all()
    for environment in environments:
        if _matches(text, environment.code, environment.aliases):
            return environment
    return None


async def resolve_addressed_team(session: AsyncSession, text: str) -> Team | None:
    teams = (await session.scalars(select(Team))).all()
    for team in teams:
        if _matches(text, team.teams_handle, team.aliases):
            return team
    return None
