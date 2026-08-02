from fastmcp import FastMCP
from backend.services.llm_tools import (
    arango_search,
    database_query,
    fetch_debate,
    fetch_documents,
    vector_search,
    vector_search_debates,
)

mcp = FastMCP("riksdagen-tools")

mcp.tool()(arango_search)
mcp.tool()(database_query)
mcp.tool()(fetch_debate)
mcp.tool()(fetch_documents)
mcp.tool()(vector_search)
mcp.tool()(vector_search_debates)

if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="127.0.0.1", port=8001, path="/")
