"""仅用于 RepoPilot MCP STDIO 集成测试的本地服务。"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP


mcp = FastMCP("RepoPilot MCP Test Server", json_response=True)


@mcp.tool()
def echo(message: str) -> dict[str, str]:
    """返回输入文本，用于验证工具发现、Schema 和调用链。"""

    return {"echo": message}


if __name__ == "__main__":
    mcp.run(transport="stdio")
