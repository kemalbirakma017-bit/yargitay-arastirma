"""
FastMCP Cloud giriş noktası.

Deploy entrypoint:  main.py:mcp   (veya doğrudan bedesten_server.py:mcp)

Bu dosya UYAP Bedesten tabanlı sunucuyu dışarı açar.
"""
from bedesten_server import mcp

__all__ = ["mcp"]

if __name__ == "__main__":
    mcp.run()
