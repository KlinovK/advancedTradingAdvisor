"""Transport inputs for account-aware limit orders and non-fill transitions."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

from advanced_trading_advisor.portfolio.models import TradingSide


class AddLimitOrderInputDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: StrictInt = Field(gt=0)
    account_id: UUID
    asset: StrictStr
    side: TradingSide
    target_price: StrictStr
    quantity_asset: StrictStr


class OrderLifecycleInputDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: StrictInt = Field(gt=0)
