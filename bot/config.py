from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    BOT_TOKEN: str
    DATABASE_URL: str = "sqlite+aiosqlite:///:memory:"

    # Groq — optional: without a key AI search (photo/voice) is disabled
    GROQ_API_KEY: str = ""
    GROQ_VISION_MODEL: str = "meta-llama/llama-4-scout-17b-16e-instruct"
    GROQ_WHISPER_MODEL: str = "whisper-large-v3-turbo"

    # Web panel
    WEB_BASE_URL: str = "http://localhost:8080"
    WEB_SECRET: str = ""  # HMAC key for login links/cookies; derived from BOT_TOKEN when empty
    WEB_LOGIN_TTL: int = 600  # seconds a /web login link stays valid
    WEB_SESSION_TTL: int = 7 * 24 * 3600


settings = Settings()
