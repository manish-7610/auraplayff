"""
Platform settings schemas — Phase 9.

PlatformSettingResponse  — single setting key/value
PlatformSettingsResponse — all settings (admin view)
UpdateSettingRequest     — PATCH body for a single setting
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class PlatformSettingResponse(BaseModel):
    key: str
    value: str
    description: Optional[str]
    is_public: bool
    updated_at: datetime
    updated_by: Optional[str]

    model_config = {"from_attributes": True}


class PlatformSettingsResponse(BaseModel):
    settings: list[PlatformSettingResponse]


class UpdateSettingRequest(BaseModel):
    value: str = Field(..., min_length=0, max_length=2000)
