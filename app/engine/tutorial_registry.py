"""
Registry for tutorial quests and progression milestones.
Exposes TUTORIAL_STEPS, eligibility verification, and claiming logic.
"""
from app.engine.tutorial import (
    TUTORIAL_STEPS,
    ensure_user_tutorial,
    is_step_eligible,
    claim_tutorial_reward,
    get_tutorial_status,
)

__all__ = [
    "TUTORIAL_STEPS",
    "ensure_user_tutorial",
    "is_step_eligible",
    "claim_tutorial_reward",
    "get_tutorial_status",
]
