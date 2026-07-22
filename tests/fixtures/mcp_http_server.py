"""仅用于 RepoPilot MCP Streamable HTTP 集成测试的本地服务。"""

from __future__ import annotations

import argparse

from mcp.server.fastmcp import FastMCP


parser = argparse.ArgumentParser()
parser.add_argument("--port", required=True, type=int)
args = parser.parse_args()

mcp = FastMCP(
    "RepoPilot MCP HTTP Test Server",
    host="127.0.0.1",
    port=args.port,
    json_response=True,
    stateless_http=True,
)


@mcp.tool()
def multiply(left: int, right: int) -> dict[str, int]:
    """返回两个整数的乘积。"""

    return {"result": left * right}


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
