"""JSON-RPC 2.0 protocol: message encoding and decoding."""

import json
from typing import Any


class JSONRPCRequest:
    """JSON-RPC 2.0 request."""

    def __init__(self, id: int | str | None, method: str, params: dict[str, Any] | None = None):
        self.id = id
        self.method = method
        self.params = params or {}

    def to_json(self) -> str:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": self.method}
        if self.id is not None:
            msg["id"] = self.id
        if self.params:
            msg["params"] = self.params
        return json.dumps(msg, ensure_ascii=False)

    @classmethod
    def from_json(cls, data: str) -> "JSONRPCRequest":
        obj = json.loads(data)
        return cls(
            id=obj.get("id"),
            method=obj["method"],
            params=obj.get("params"),
        )


class JSONRPCResponse:
    """JSON-RPC 2.0 response (result or error)."""

    def __init__(
        self,
        id: int | str | None,
        result: Any = None,
        error: dict[str, Any] | None = None,
    ):
        self.id = id
        self.result = result
        self.error = error

    def to_json(self) -> str:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": self.id}
        if self.error is not None:
            msg["error"] = self.error
        else:
            msg["result"] = self.result
        return json.dumps(msg, ensure_ascii=False)

    @classmethod
    def from_json(cls, data: str) -> "JSONRPCResponse":
        obj = json.loads(data)
        return cls(
            id=obj.get("id"),
            result=obj.get("result"),
            error=obj.get("error"),
        )


# Standard JSON-RPC error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602

# Application-specific error codes
ELEMENT_NOT_FOUND = -1001
COORDINATE_OUT_OF_RANGE = -1002
WAIT_TIMEOUT = -1003
PERMISSION_DENIED = -1004
VISION_API_ERROR = -1005
DAEMON_INTERNAL_ERROR = -1999


def make_error(code: int, message: str, id: int | str | None = None) -> JSONRPCResponse:
    return JSONRPCResponse(id=id, error={"code": code, "message": message})
