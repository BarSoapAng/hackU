from dataclasses import dataclass, fields
import os


@dataclass(frozen=True)
class Limits:
    projects_per_user: int = 5
    projects_total: int = 100
    turns_per_minute: int = 10
    turns_per_day: int = 100
    generations_per_day: int = 1000
    generations_per_turn: int = 4
    tools_per_turn: int = 3
    pending_per_user: int = 10
    pending_total: int = 100
    files_per_project: int = 20
    file_bytes: int = 131072
    project_bytes: int = 1048576
    chunks_per_project: int = 1000
    embedding_tokens_per_day: int = 100000
    indexing_attempts_per_hour: int = 5
    turn_seconds: int = 120
    lease_seconds: int = 60
    link_seconds: int = 3600
    dedup_days: int = 30

    @classmethod
    def from_env(cls):
        values = {f.name: int(os.environ.get('LIMIT_' + f.name.upper(), f.default)) for f in fields(cls)}
        if any(v < 1 for v in values.values()) or values['dedup_days'] < 30:
            raise ValueError('Limits must be positive; dedup retention must be at least 30 days')
        return cls(**values)


@dataclass(frozen=True)
class Settings:
    public_url: str
    photon_project_id: str
    photon_api_key: str
    photon_webhook_secret: str
    database_url: str
    database_name: str
    database_token: str
    nemotron_base_url: str
    nemotron_api_key: str
    openai_api_key: str
    encryption_keys: str
    active_key: str = 'v1'
    nemotron_model: str = 'nemotron-builder'
    project_model: str = 'gpt-5.6-luna'

    @classmethod
    def from_env(cls):
        values = {}
        for field in fields(cls):
            value = os.environ.get(field.name.upper())
            if value is not None:
                values[field.name] = value
        settings = cls(**values)
        if not settings.public_url.startswith('https://'):
            raise ValueError('PUBLIC_URL must use HTTPS')
        if settings.project_model != 'gpt-5.6-luna':
            raise ValueError('A project model change requires an explicit deployment decision')
        return settings
