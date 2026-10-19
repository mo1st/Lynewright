from mcp.server.fastmcp import FastMCP

mcp = FastMCP("Lynewright MCP example", streamable_http_path="/")


@mcp.tool()
def echo(query: str) -> dict[str, str]:
    """Return the supplied text as structured content."""
    return {"value": query}


app = mcp.streamable_http_app()
