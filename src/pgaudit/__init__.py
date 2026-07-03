"""pgaudit - MCP server for PostgreSQL performance auditing."""

__version__ = "0.1.0"


def main():
    from pgaudit.server import mcp

    mcp.run()
