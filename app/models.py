from pydantic import BaseModel, Field
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

class OrderCreate(BaseModel):
    order_type: str = Field(..., pattern="^(BUY|SELL)$")
    resource_type: str
    amount: float = Field(..., gt=0)
    limit_price: float = Field(..., gt=0)

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
