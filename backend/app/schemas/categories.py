import uuid
from datetime import datetime
from typing import Literal

from pydantic import Field

from app.schemas.common import ApiModel, PageMeta

CategoryType = Literal["income", "expense", "both"]


class CategoryCreate(ApiModel):
    parent_id: uuid.UUID | None = None
    name: str = Field(min_length=1, max_length=200)
    category_type: CategoryType
    color: str | None = Field(default=None, max_length=20)
    icon: str | None = Field(default=None, max_length=50)
    sort_order: int = 0


class CategoryUpdate(ApiModel):
    version: int = Field(ge=1)
    parent_id: uuid.UUID | None = None
    name: str | None = Field(default=None, min_length=1, max_length=200)
    category_type: CategoryType | None = None
    color: str | None = Field(default=None, max_length=20)
    icon: str | None = Field(default=None, max_length=50)
    sort_order: int | None = None
    is_archived: bool | None = None


class CategoryResponse(ApiModel):
    id: uuid.UUID
    parent_id: uuid.UUID | None
    name: str
    category_type: CategoryType
    color: str | None
    icon: str | None
    sort_order: int
    is_archived: bool
    version: int
    created_at: datetime
    updated_at: datetime


class CategoryTreeItem(CategoryResponse):
    children: list["CategoryTreeItem"] = Field(default_factory=list)


class CategoryPage(ApiModel):
    items: list[CategoryResponse]
    page: PageMeta
