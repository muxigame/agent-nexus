"""Shared foundation for the WorkSpace/mcp servers: FastMCP 4, one config file, one auth module."""

from .config import ConfigError, ServiceConfig
from .service import Service, main

__version__ = "0.2.0"
__all__ = ["ConfigError", "Service", "ServiceConfig", "main"]
