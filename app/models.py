from typing import Literal

from pydantic import BaseModel, Field


class UserCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    email: str = Field(min_length=3, max_length=320)
    phone: str = Field(min_length=3, max_length=40)
    kyc_status: Literal["pending", "verified", "rejected"] = "pending"


class WalletCreate(BaseModel):
    label: str = Field(min_length=1, max_length=80)


class TransferCreate(BaseModel):
    type: Literal["top_up", "peer_transfer", "withdrawal", "fee", "reversal"]
    amount_paise: int = Field(gt=0)
    source_wallet_id: int | None = None
    destination_wallet_id: int | None = None
    original_transfer_id: int | None = None
    note: str | None = Field(default=None, max_length=500)


class ReleaseRequest(BaseModel):
    actor: str = Field(default="admin", min_length=1, max_length=120)


class EmptyRequest(BaseModel):
    pass


class AskRequest(BaseModel):
    question: str = Field(min_length=3, max_length=1000)
