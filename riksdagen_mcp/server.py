from fastmcp import FastMCP

from backend.services.llm_tools import (
    database_query,
    fetch_debate,
    fetch_speeches,
    search_speeches,
    vector_search,
    vector_search_debates,
)

mcp = FastMCP("riksdagen-tools")

mcp.tool()(search_speeches)
mcp.tool()(database_query)
mcp.tool()(fetch_debate)
mcp.tool()(fetch_speeches)
mcp.tool()(vector_search)
mcp.tool()(vector_search_debates)

if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="127.0.0.1", port=8001, path="/")
