from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "sqlite+aiosqlite:///./ficheassur.db"
    allowed_origins: str = "http://localhost:3100"
    target_region: str = "FR"
    blocked_ips: str = ""
    # Signs the espace pro's login tokens. MUST be set to a long random value
    # in production (e.g. python -c "import secrets; print(secrets.token_urlsafe(48))").
    secret_key: str = "dev-only-change-me"
    # Public site address, used to build broker activation links.
    site_url: str = "http://localhost:3100"

    @property
    def origins_list(self) -> list[str]:
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]

    @property
    def blocked_ips_set(self) -> set[str]:
        return {ip.strip() for ip in self.blocked_ips.split(",") if ip.strip()}

    class Config:
        env_file = ".env"


settings = Settings()
