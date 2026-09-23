"""MCP（SSE）サーバー: ツール定義はチャットと同じ CLAUDE_TOOLS，実行は tools の dispatcher（CLAUDE.md §3.2）．
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

from fastapi import APIRouter
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.types import TextContent, Tool
from starlette.requests import Request

from .prompts import CLAUDE_TOOLS
from .results import results_store
from .tools import _collect_tool_results, _ToolTurnState

# ──────────────────────────────────────────────
# MCP サーバー定義
# ──────────────────────────────────────────────
mcp_server = Server("uxsim-mcp")

def _mcp_tools() -> list[Tool]:
    """MCP に公開するツール．チャット（CLAUDE_TOOLS）と同じ定義を変換して返す．

    以前は MCP 専用に run_simulation / get_result の 2 つだけを別定義していたため，
    差分再実行・ネットワーク照会・OSM 取込が MCP から使えず，説明も二重管理だった．
    get_result は互換のために残す（統計だけを返す軽量ツール）．
    """
    tools = [Tool(name=t["name"], description=t["description"], inputSchema=t["input_schema"])
             for t in CLAUDE_TOOLS]
    tools.append(Tool(
        name="get_result",
        description="実行済みシミュレーションの統計（total_trips / completed_trips / "
                    "average_travel_time_s）を ID で取得する．詳細は get_simulation_data を使う．",
        inputSchema={
            "type": "object",
            "properties": {
                "simulation_id": {"type": "string", "description": "run_simulation が返した sim_id"},
            },
            "required": ["simulation_id"],
        },
    ))
    return tools


async def _mcp_call_tool(name: str, arguments: dict | None) -> str:
    """MCP のツール呼び出し本体．チャットと同じ _dispatch_tool_blocks を通す（§3.2）．

    進捗イベントは MCP に流す先が無いので捨てる（_collect_tool_results）．
    会話コンテキスト（body）は無いので None．保存メタの via は "mcp"．
    """
    arguments = arguments or {}
    if name == "get_result":
        sim_id = str(arguments.get("simulation_id", ""))
        if sim_id not in results_store:
            return f"ID {sim_id} の結果が見つかりません．"
        return json.dumps(results_store[sim_id]["stats"], ensure_ascii=False)

    block = SimpleNamespace(id=f"mcp-{uuid.uuid4().hex[:8]}", name=name, input=arguments)
    state = _ToolTurnState(body=None, via="mcp")
    results = await _collect_tool_results([block], state)
    return results[0]["content"]   # 未知のツール名でも dispatcher が結果を返す


@mcp_server.list_tools()
async def list_tools() -> list[Tool]:
    return _mcp_tools()


@mcp_server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    return [TextContent(type="text", text=await _mcp_call_tool(name, arguments))]


# ---- MCP SSE エンドポイント ----

router = APIRouter()
sse_transport = SseServerTransport("/mcp/messages/")

@router.get("/mcp")
async def mcp_sse(request: Request):
    async with sse_transport.connect_sse(
        request.scope, request.receive, request._send
    ) as streams:
        await mcp_server.run(
            streams[0], streams[1], mcp_server.create_initialization_options()
        )

@router.post("/mcp/messages/")
async def mcp_messages(request: Request):
    await sse_transport.handle_post_message(request.scope, request.receive, request._send)
