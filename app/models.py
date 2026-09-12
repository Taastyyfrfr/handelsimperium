from pydantic import BaseModel, Field, field_validator
from typing import Optional, List
from datetime import datetime

class UserCreate(BaseModel):
    username: str = Field(..., min_length=3, max_length=32)
    password: str = Field(..., min_length=6, max_length=128)

class UserLogin(BaseModel):
    username: str
    password: str

class UserResponse(BaseModel):
    id: int
    username: str
    balance: float
    created_at: datetime

class InventoryItem(BaseModel):
    resource_type: str
    amount: float
    storage_cap: float
    production_rate: float
    last_calculated_at: datetime

class BuildingItem(BaseModel):
    building_type: str
    name: str
    resource_type: str
    level: int
    production_rate: float
    upgrade_cost: float

from app.config import SUPPORTED_RESOURCES

class OrderCreate(BaseModel):
    order_type: str = Field(..., pattern="^(BUY|SELL)$")
    resource_type: str = Field(...)
    amount: int = Field(..., ge=1, le=100_000, description="Positive integer quantity, min 1, max 100,000")
    limit_price: float = Field(..., gt=0.0, le=1_000_000.0, description="Limit price between 0.01 and 1,000,000 Taler")

    @field_validator("resource_type")
    @classmethod
    def validate_resource(cls, v: str) -> str:
        v = v.lower()
        if v not in SUPPORTED_RESOURCES:
            raise ValueError(f"Ungültige Handelsware: {v}. Erlaubt: {', '.join(SUPPORTED_RESOURCES)}")
        return v

    @field_validator("amount")
    @classmethod
    def validate_amount(cls, v: int) -> int:
        if v < 1:
            raise ValueError("Menge muss mindestens 1 ganze Einheit betragen.")
        return v

    @field_validator("limit_price")
    @classmethod
    def validate_limit_price(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("Limitpreis muss größer als 0 sein.")
        if v > 1_000_000.0:
            raise ValueError("Limitpreis darf maximal 1.000.000,00 Taler betragen.")
        return round(float(v), 2)


class MarketOrderItem(BaseModel):
    id: int
    user_id: int
    order_type: str
    resource_type: str
    amount: float
    filled_amount: float
    limit_price: float
    status: str
    created_at: datetime
    is_own: bool = False

class TradeItem(BaseModel):
    id: int
    buyer_username: str
    seller_username: str
    resource_type: str
    amount: float
    price: float
    fee: float
    total_value: float
    executed_at: datetime
