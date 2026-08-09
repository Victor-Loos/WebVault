import re

from pydantic import BaseModel, Field, field_validator

WAIT_UNTIL_OPTIONS = {
    "domcontentloaded",
    "load",
    "networkidle0",
    "networkidle2",
    "load,networkidle2",
}


class CrawlConfig(BaseModel):
    seeds: list[str] = Field(min_length=1, max_length=20)
    collection: str | None = Field(default=None, max_length=100)
    profileId: str | None = Field(default=None, max_length=160, pattern=r"^[a-z0-9][a-z0-9-]*$")
    interactive: bool = False
    workers: int = 1
    depth: int = 1
    pageLimit: int = Field(default=1_000, ge=1, le=100_000)
    timeLimit: int = Field(default=3_600, ge=1, le=86_400)
    scopeType: str = "prefix"
    behaviors: list[str] = Field(
        default_factory=lambda: ["autoscroll", "autoplay", "autofetch", "siteSpecific"]
    )
    screenshot: str | None = None
    screencastPort: int | None = None
    waitUntil: str = "load"
    generateWACZ: bool = True
    generateCDX: bool = True
    combineWARC: bool = True
    headless: bool = True
    text: list[str] = Field(default_factory=lambda: ["to-pages"])
    include: list[str] = Field(default_factory=list, max_length=50)
    exclude: list[str] = Field(default_factory=list, max_length=50)
    scheduleInterval: str = "none"
    retentionCount: int = Field(default=0, ge=0, le=1_000)

    @field_validator("seeds")
    @classmethod
    def validate_seeds(cls, value: list[str]):
        if any(len(seed) > 2_048 for seed in value):
            raise ValueError("seed URLs must not exceed 2048 characters")
        return value

    @field_validator("pageLimit", mode="before")
    @classmethod
    def default_page_limit(cls, value: int):
        return 1_000 if value == 0 else value

    @field_validator("timeLimit", mode="before")
    @classmethod
    def default_time_limit(cls, value: int):
        return 3_600 if value == 0 else value

    @field_validator("workers")
    @classmethod
    def validate_workers(cls, value: int):
        if value < 1 or value > 10:
            raise ValueError("workers must be between 1 and 10")
        return value

    @field_validator("depth")
    @classmethod
    def validate_depth(cls, value: int):
        if value < 0 or value > 5:
            raise ValueError("depth must be between 0 and 5")
        return value

    @field_validator("waitUntil")
    @classmethod
    def validate_wait_until(cls, value: str):
        if value not in WAIT_UNTIL_OPTIONS:
            raise ValueError("unsupported waitUntil value")
        return value

    @field_validator("scopeType")
    @classmethod
    def validate_scope_type(cls, value: str):
        if value not in {"page", "page-spa", "prefix", "host", "domain", "any"}:
            raise ValueError("unsupported scopeType value")
        return value

    @field_validator("include", "exclude")
    @classmethod
    def validate_url_rules(cls, value: list[str]):
        for pattern in value:
            if not pattern.strip() or len(pattern) > 1_024:
                raise ValueError("URL rules must be between 1 and 1024 characters")
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"Invalid URL rule: {pattern}") from exc
        return value

    @field_validator("scheduleInterval")
    @classmethod
    def validate_schedule_interval(cls, value: str):
        if value not in {"none", "daily", "weekly", "monthly"}:
            raise ValueError("unsupported schedule interval")
        return value

    @field_validator("behaviors")
    @classmethod
    def validate_behaviors(cls, value: list[str]):
        allowed = {"autoscroll", "autoplay", "autofetch", "siteSpecific", "autoclick"}
        if any(item not in allowed for item in value):
            raise ValueError("unsupported browser behavior")
        return value


class ProfileCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    url: str = Field(min_length=1, max_length=2_048)


class RenameCollectionRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class MoveFileRequest(BaseModel):
    collection: str = Field(min_length=1, max_length=100)
