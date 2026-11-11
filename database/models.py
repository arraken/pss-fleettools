from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Field, SQLModel

class Engagement(SQLModel, table=True):
    __tablename__ = "engagements"

    engagement_id: int = Field(primary_key=True)
    system_id: int = Field(index=True)
    attacker: str = Field(index=True)
    defender: str = Field(index=True)
    engagement_type: str
    start_time: datetime = Field(index=True)
    end_time: datetime = Field(index=True)
    outcome: str
    final_score: str
    active: bool = Field(default=True, index=True)
    last_checked: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class GalaxySystem(SQLModel, table=True):
    __tablename__ = "galaxy_systems"

    system_id: int = Field(primary_key=True)
    system_name: str = Field(index=True)
    owner_name: Optional[str] = Field(default=None, index=True)
    cooldown_end: Optional[datetime] = Field(default=None, index=True)
    last_updated: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), index=True)

    # Active engagement state
    under_attack: bool = Field(default=False, index=True)
    active_engagement_id: Optional[int] = Field(default=None)

    # # Fleet wars targeting info
    is_targeted: bool = Field(default=False, index=True)
    # targeting_fleet: Optional[str] = Field(default=None)
    # flagged_by: Optional[int] = Field(default=None)
    # admin_role_id: Optional[int] = Field(default=None)
    # flagged_at: Optional[datetime] = Field(default=None)


class FleetRoleMappingDB(SQLModel, table=True):
    """Maps a fleet name to a Discord role ID for engagement alerts.
    Loaded dynamically; populate via admin commands or direct DB edits."""
    __tablename__ = "fleet_role_mappings"

    id: Optional[int] = Field(default=None, primary_key=True)
    fleet_name: str = Field(index=True, unique=True)
    admin_role_id: int
    guild_id: Optional[int] = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class AlertChannelDB(SQLModel, table=True):
    """Stores Discord channel IDs for various alert types (engagements, galaxy, etc.).
    Loaded dynamically; populate via admin commands or direct DB edits."""
    __tablename__ = "alert_channels"

    id: Optional[int] = Field(default=None, primary_key=True)
    guild_id: int = Field(index=True)
    channel_id: int
    channel_type: str = Field(default="engagements")  # e.g. "engagements", "galaxy"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class SystemTargetDB(SQLModel, table=True):
    """Stores targeting data by fleet ID and system ID"""
    __tablename__ = "system_targets"

    id: Optional[int] = Field(default=None, primary_key=True)
    system_id: int = Field(foreign_key="galaxy_systems.system_id", index=True)
    targeting_fleet_id: int = Field(foreign_key="fleet_role_mappings.id", index=True)
    flagged_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class PlannerProfile(SQLModel, table=True):
    """One prestige-planner roster per Discord user + PSS player name."""
    __tablename__ = "planner_profiles"

    id: Optional[int] = Field(default=None, primary_key=True)
    discord_id: int = Field(index=True)
    player_name: str = Field(index=True)  # lower-cased lookup key
    display_name: str
    last_synced: Optional[datetime] = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class PlannerCrew(SQLModel, table=True):
    """A crew in the planner roster. Virtual rows are crew produced by a planned (not yet done) prestige."""
    __tablename__ = "planner_crew"

    id: Optional[int] = Field(default=None, primary_key=True)
    profile_id: int = Field(foreign_key="planner_profiles.id", index=True)
    crew_id: Optional[str] = Field(default=None, index=True)  # in-game id; None while virtual
    design_id: int = Field(index=True)
    design_name: str
    crew_name: str
    level: Optional[int] = Field(default=None)  # None = unknown ("?")
    rarity: str = ""
    virtual: bool = Field(default=False)
    gone: bool = Field(default=False)  # real crew no longer on the live ship (kept only while a step uses it)
    used_in_step: Optional[int] = Field(default=None, index=True)  # planner_steps.id consuming/claiming this crew


class PlannerStep(SQLModel, table=True):
    """A planned prestige: two roster crew consumed -> one result crew."""
    __tablename__ = "planner_steps"

    id: Optional[int] = Field(default=None, primary_key=True)
    profile_id: int = Field(foreign_key="planner_profiles.id", index=True)
    step_no: int
    target_design_id: int
    target_name: str
    crew1_row_id: Optional[int] = Field(default=None)
    crew2_row_id: Optional[int] = Field(default=None)
    result_row_id: Optional[int] = Field(default=None)
    crew1_label: str = ""
    crew2_label: str = ""
    status: str = Field(default="planned", index=True)  # planned | missing | pending_confirm | done
    matched_row_id: Optional[int] = Field(default=None)  # real crew detected as this step's result
    rejected_crew_ids: str = ""  # comma-separated crew ids the user said are not this step's result
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
