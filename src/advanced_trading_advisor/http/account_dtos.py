"""Transport shape for creating a confirmed portfolio account."""

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

from advanced_trading_advisor.portfolio.models import AccountType


class AccountPositionInputDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: StrictStr
    amount: StrictStr


class CreateAccountInputDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: StrictInt = Field(gt=0)
    name: StrictStr
    account_type: AccountType
    positions: tuple[AccountPositionInputDTO, ...]
