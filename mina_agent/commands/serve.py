"""MCP server over stdio (hidden). This is what `claude mcp add` registers."""


def serve():
    """Run the mina-harness MCP server on stdio."""
    from .. import server
    server.server.run(transport="stdio")
