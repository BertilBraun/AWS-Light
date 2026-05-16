from __future__ import annotations

from pydantic import BaseModel


class SecretSpec(BaseModel):
    name: str
    value: str


class CreateSecretRequest(BaseModel):
    name: str
    value: str
