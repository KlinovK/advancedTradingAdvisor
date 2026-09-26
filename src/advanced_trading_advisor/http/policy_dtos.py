"""Transport shapes for global confirmed-portfolio policy updates."""

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr


class UpdateFinancialSettingsInputDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: StrictInt = Field(gt=0)
    monthly_expenses_usd: StrictStr
    target_expense_runway_months: StrictStr


class UpdateCorePositionInputDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: StrictInt = Field(gt=0)
    hard_floor: StrictStr
    preferred_quantity: StrictStr
