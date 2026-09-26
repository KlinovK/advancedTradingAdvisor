"""Transport inputs for account-aware limit orders and explicit lifecycle operations."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

from advanced_trading_advisor.portfolio.models import TradingSide

from .account_dtos import AccountPositionInputDTO


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


class ConfirmLimitOrderFilledInputDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: StrictInt = Field(gt=0)
    settlement_asset: StrictStr
    positions: tuple[AccountPositionInputDTO, ...]
