"""pgtriage - MCP server for PostgreSQL performance auditing."""

__version__ = "0.3.0"


def main():
    from pgtriage.server import mcp

    mcp.run()
