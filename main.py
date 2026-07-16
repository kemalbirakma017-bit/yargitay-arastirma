"""
FastMCP Cloud giriş noktası.

Deploy entrypoint:  main.py:mcp   (veya doğrudan server.py:mcp)

Tüm mantık server.py içindedir; bu dosya yalnızca sunucuyu dışarı açar.
"""
from server import mcp

__all__ = ["mcp"]

if __name__ == "__main__":
    mcp.run()
