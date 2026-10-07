from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    BOT_TOKEN: str
    DATABASE_URL: str = "sqlite+aiosqlite:///:memory:"
    GROQ_API_KEY: str
    VISION_MODEL: str = "meta-llama/llama-4-scout-17b-16e-instruct"
    WHISPER_MODEL: str = "whisper-large-v3-turbo"


settings = Settings()
