"""pgtriage - MCP server for PostgreSQL performance auditing."""

__version__ = "0.2.1"


def main():
    from pgtriage.server import mcp

    mcp.run()
