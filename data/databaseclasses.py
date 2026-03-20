from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from data.database_models import Engagement, GalaxySystem


@dataclass
class EngagementSystemData:
    active: bool
    attacker: str
    defender: str
    engagement_id: int
    system_id: int
    start_time: datetime
    end_time: datetime
    outcome: str
    final_score: str
    engagement_type: str

    def to_db_model(self) -> Engagement:
        start = _ensure_aware(self.start_time)
        end = _ensure_aware(self.end_time)
        return Engagement(
            engagement_id=self.engagement_id,
            system_id=self.system_id,
            attacker=self.attacker,
            defender=self.defender,
            engagement_type=self.engagement_type,
            start_time=self.start_time,
            end_time=self.end_time,
            outcome=self.outcome,
            final_score=self.final_score,
            active=self.active,
            last_checked=datetime.now(timezone.utc)
        )

    @classmethod
    def from_db_model(cls, db_engagement: Engagement) -> "EngagementSystemData":
        start = _ensure_aware(db_engagement.start_time)
        end = _ensure_aware(db_engagement.end_time)
        return cls(
            engagement_id=db_engagement.engagement_id,
            system_id=db_engagement.system_id,
            attacker=db_engagement.attacker,
            defender=db_engagement.defender,
            engagement_type=db_engagement.engagement_type,
            start_time=db_engagement.start_time,
            end_time=db_engagement.end_time,
            outcome=db_engagement.outcome,
            final_score=db_engagement.final_score,
            active=db_engagement.active
        )


@dataclass
class FleetWarsSystem:
    name: str
    star_system_id: int
    targeting_fleet: str  # Fleet name that is targeting this system
    flagged_by: int  # Discord user ID who flagged this
    flagged_at: datetime
    cooldown_end: datetime
    last_api_check: datetime
    owner_name: str
    admin_role_id: Optional[int] = None


def _ensure_aware(dt: datetime) -> datetime:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)