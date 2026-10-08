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
    # Close the libvirt connection after this many idle minutes when no browser is attached
    # (live events), so socket-activated libvirtd / virtqemud can exit (their --timeout). 0 = never.
    LIBVIRT_IDLE_TIMEOUT: float = 5
    # How libvirt runs on this host, for Start/Stop: auto | monolithic (libvirtd) | modular (virtqemud…)
    LIBVIRT_DAEMON_MODE: str = "auto"
    # Root-owned helper run through pkexec (installed by scripts/setup.sh), e.g. for DHCP release
    HELPER_PATH: str = "/usr/libexec/vm-manager/helper"

    # Storage pool used for new VM disks and uploaded ISOs (created if missing)
    DEFAULT_POOL_NAME: str = "default"
    DEFAULT_POOL_PATH: str = "/var/lib/libvirt/images"

    # Network attached to new VMs when none is given
    DEFAULT_NETWORK: str = "default"

    # Kubernetes clusters get their own NAT network: the first free /24 of this range
    CLUSTER_SUBNET_POOL: str = "10.43.0.0/16"

    # WireGuard remote access to lab groups. The app relays UDP from the host to each group router
    # (their uplink is NATed by libvirt): one port of WG_HOST_PORTS per group, on WG_RELAY_LISTEN.
    # Open that range (udp) in the host firewall: scripts/setup.sh does it for ufw / firewalld.
    WG_HOST_PORTS: str = "51820-51869"
    WG_RELAY_LISTEN: str = "0.0.0.0"
    # Host name / address put in client configs ("Endpoint"); empty = the host's primary LAN address
    WG_ENDPOINT_HOST: str = ""
    # Tunnel subnets: the first free /24 of this range for each group
    WG_SUBNET_POOL: str = "10.44.0.0/16"
    # BGP announce ranges (addresses lab machines announce to their group router, e.g. a MetalLB BGP
    # pool): the first free /27 of this range, unique on the host so WireGuard clients can route them
    BGP_ANNOUNCE_POOL: str = "10.45.0.0/16"
    # Dual stack lab groups (docs/ipv6.md): every IPv6 /64 the app assigns (group networks, WireGuard tunnels,
    # BGP announce ranges) is a free /64 of this ULA range, unique on the host
    IPV6_ULA_POOL: str = "fd00:564d:4d00::/48"

    # Address VNC consoles listen on. 127.0.0.1 keeps them local to the host;
    # set to 0.0.0.0 to reach them from the LAN (they have no password).
    VNC_LISTEN: str = "127.0.0.1"

    # CORS
    CORS_ORIGINS: list[str] = ["http://localhost:3000", "http://localhost:5173"]


settings = Settings()
