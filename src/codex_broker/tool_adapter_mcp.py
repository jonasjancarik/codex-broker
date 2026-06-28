from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


class ToolAdapterServer:
    def __init__(self, config_path: Path) -> None:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        self.tools = {tool["name"]: tool for tool in payload.get("tools", [])}
        self.broker_context = payload.get("brokerContext") if isinstance(payload.get("brokerContext"), dict) else {}

    def run(self) -> None:
        for line in sys.stdin:
            if not line.strip():
                continue
            try:
                message = json.loads(line)
                response = self.handle(message)
            except Exception as exc:  # noqa: BLE001 - this is a protocol boundary.
                response = {"id": None, "error": {"code": -32603, "message": str(exc)}}
            if response is not None:
                sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
                sys.stdout.flush()

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        method = message.get("method")
        request_id = message.get("id")
        if method == "notifications/initialized":
            return None
        if method == "initialize":
            return {
                "id": request_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "codex-broker-tool-adapter", "version": "0.4.0"},
                },
            }
        if method == "tools/list":
            return {
                "id": request_id,
                "result": {
                    "tools": [
                        {
                            "name": tool["name"],
                            "description": tool.get("description", ""),
                            "inputSchema": tool.get("inputSchema") or {"type": "object"},
                        }
                        for tool in self.tools.values()
                    ]
                },
            }
        if method == "tools/call":
            params = message.get("params") if isinstance(message.get("params"), dict) else {}
            return {"id": request_id, "result": self.call_tool(params)}
        return {"id": request_id, "error": {"code": -32601, "message": f"Unsupported method: {method}"}}

    def call_tool(self, params: dict[str, Any]) -> dict[str, Any]:
        name = str(params.get("name") or "")
        tool = self.tools.get(name)
        if not tool:
            return self.content(f"Unknown tool: {name}", is_error=True)
        payload = {
            "tool": name,
            "arguments": params.get("arguments") if isinstance(params.get("arguments"), dict) else {},
            "context": {
                "broker": self.broker_context,
                "tool": tool.get("context") if isinstance(tool.get("context"), dict) else {},
                "policy": {
                    "approvalPolicy": tool.get("approvalPolicy") or "never",
                    "scope": tool.get("scope") or "owner",
                    "networkPolicy": tool.get("networkPolicy") or {"mode": "host-allowlist"},
                },
            },
        }
        data = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Codex-Broker-Tool": name,
        }
        declared_headers = tool.get("headers") if isinstance(tool.get("headers"), dict) else {}
        for key, value in declared_headers.items():
            text = str(value)
            if text.startswith("env:"):
                env_name = text.removeprefix("env:")
                env_value = os.environ.get(env_name)
                if env_value is None:
                    return self.content(f"Missing hosted tool header environment variable: {env_name}", is_error=True)
                text = env_value
            headers[str(key)] = text
        req = urllib.request.Request(
            str(tool["endpoint"]),
            data=data,
            method="POST",
            headers=headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=float(tool.get("timeoutSeconds") or 30)) as response:
                body = response.read().decode("utf-8", errors="replace")
                content_type = response.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            return self.content(body or f"HTTP {exc.code}", is_error=True)
        except urllib.error.URLError as exc:
            return self.content(str(exc), is_error=True)
        if "application/json" in content_type:
            try:
                parsed = json.loads(body)
                direct_result = self.mcp_result(parsed)
                if direct_result is not None:
                    return direct_result
                body = json.dumps(parsed, ensure_ascii=False, indent=2)
            except json.JSONDecodeError:
                pass
        return self.content(body)

    @staticmethod
    def mcp_result(payload: Any) -> dict[str, Any] | None:
        if not isinstance(payload, dict) or not isinstance(payload.get("content"), list):
            return None
        result: dict[str, Any] = {"content": payload["content"]}
        if isinstance(payload.get("isError"), bool):
            result["isError"] = payload["isError"]
        if "structuredContent" in payload:
            result["structuredContent"] = payload["structuredContent"]
        if isinstance(payload.get("_meta"), dict):
            result["_meta"] = payload["_meta"]
        return result

    @staticmethod
    def content(text: str, *, is_error: bool = False) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": text}], "isError": is_error}


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m codex_broker.tool_adapter_mcp <config.json>")
    ToolAdapterServer(Path(sys.argv[1])).run()


if __name__ == "__main__":
    main()
