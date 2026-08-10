"""Validated models for the Atlas test-control API."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ApiModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class ControlEnvelope[DataT](ApiModel):
    code: int
    success: bool
    message: str
    uuid: str | None = None
    data: DataT | None = None
    time: str


class AuthTokenCreated(ApiModel):
    token: str = Field(alias="cliAuthToken")
    expires_at: str = Field(alias="expiresAt")
    request_id: str | None = None


class AuthTokenStatus(ApiModel):
    status: str
    message: str
    request_id: str | None = None
    retry_after_seconds: float | None = None


class ExchangedCredentials(ApiModel):
    jwt: str = Field(alias="token")
    client_code: str = Field(alias="clientCode")
    cid: str
    request_id: str | None = None


class RefreshedSession(ApiModel):
    token: str = Field(min_length=1, repr=False)
    expire_seconds: int = Field(alias="expireSecond", gt=0)
    request_id: str | None = None


class ClientStatus(ApiModel):
    activation_status: int = Field(alias="activationStatus")


class TopUpStatus(ApiModel):
    completed: bool


class AccessInfoStatus(ApiModel):
    exists: bool


class AccessInfoPayload(ApiModel):
    client_status: ClientStatus = Field(alias="clientStatus")
    top_up: TopUpStatus = Field(alias="topUp")
    access_info: AccessInfoStatus = Field(alias="accessInfo")


class AccessInfo(ApiModel):
    activation_status: int
    top_up_completed: bool
    access_info_exists: bool
    request_id: str | None = None


class AccessCredentialRecord(ApiModel):
    client_code: str | None = Field(default=None, alias="clientCode")
    ak: str = Field(repr=False)
    sk: str = Field(repr=False)
    expiry_date: str | None = Field(default=None, alias="expiryDate")


class PreProductionAccessInfos(ApiModel):
    sandbox: list[AccessCredentialRecord]
    pre: list[AccessCredentialRecord]
    request_id: str | None = None


class ProductionAccessInfos(ApiModel):
    sandbox: list[AccessCredentialRecord] = Field(default_factory=list)
    prd: list[AccessCredentialRecord] = Field(default_factory=list)
    request_id: str | None = None


class FareSearchUsage(ApiModel):
    daily_limit: int = Field(alias="dailyLimit")
    used_today: int = Field(alias="usedToday")
    request_id: str | None = None


class ServerVersion(ApiModel):
    version: str
    request_id: str | None = None
