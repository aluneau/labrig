"""Configuration management for VM Manager"""
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings (overridable via environment or backend/.env)"""

    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[1] / ".env", env_file_encoding="utf-8"
    )

    # App
    APP_NAME: str = "VM Manager"
    APP_VERSION: str = "0.1.0"
    DEBUG: bool = False

    # Server
    HOST: str = "0.0.0.0"
    PORT: int = 8000

    # Database
    DATABASE_URL: str = f"sqlite:///{Path(__file__).resolve().parents[1] / 'data' / 'vmanager.db'}"
    DATA_DIR: Path = Path(__file__).resolve().parents[1] / "data"

    # Built frontend served at / (cd frontend && npm run build)
    FRONTEND_DIR: Path = Path(__file__).resolve().parents[2] / "frontend" / "build"

    # libvirt
    LIBVIRT_URI: str = "qemu:///system"

    # Storage pool used for new VM disks and uploaded ISOs (created if missing)
    DEFAULT_POOL_NAME: str = "default"
    DEFAULT_POOL_PATH: str = "/var/lib/libvirt/images"

    # Network attached to new VMs when none is given
    DEFAULT_NETWORK: str = "default"

    # Address VNC consoles listen on. 127.0.0.1 keeps them local to the host;
    # set to 0.0.0.0 to reach them from the LAN (they have no password).
    VNC_LISTEN: str = "127.0.0.1"

    # CORS
    CORS_ORIGINS: list[str] = ["http://localhost:3000", "http://localhost:5173"]


settings = Settings()
