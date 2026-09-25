"""Transport shapes for confirmed portfolio account mutations."""

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


class RenameAccountInputDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: StrictInt = Field(gt=0)
    name: StrictStr


class UpdateAccountHoldingsInputDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: StrictInt = Field(gt=0)
    positions: tuple[AccountPositionInputDTO, ...]


class DeleteAccountInputDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: StrictInt = Field(gt=0)
