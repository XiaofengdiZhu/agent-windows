"""JSON-RPC 2.0 protocol: message encoding and decoding."""

import json
from typing import Any


class JSONRPCRequest:
    """JSON-RPC 2.0 request."""

    def __init__(self, id: int | str | None, method: str, params: dict[str, Any] | None = None):
        # Validate field types at construction so EVERY request — whether parsed
        # from the wire (from_json), built from a batch sub-command, or assembled
        # by the CLI/dispatcher — is well-formed. A non-dict params (e.g. a list)
        # would leak to a handler as the wrong type; a non-string method would
        # key the handler registry wrongly; and a non-hashable id (a list/dict)
        # would crash COMWorkerThread.submit (which carries id in the queue dict
        # and echoes it in error responses) and drop the connection with no
        # JSON-RPC error sent back. Raises ValueError so the server maps it to
        # INVALID_REQUEST / INVALID_PARAMS. (M-08)
        if not isinstance(method, str):
            raise ValueError("JSON-RPC 'method' must be a string")
        if params is not None and not isinstance(params, dict):
            raise ValueError("JSON-RPC 'params' must be an object")
        if id is not None and not isinstance(id, (str, int)):
            raise ValueError("JSON-RPC 'id' must be a string or number")
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
        # json.loads raises json.JSONDecodeError for non-JSON input (→ PARSE_ERROR
        # at the server). The body-is-dict check rejects valid JSON that isn't a
        # Request object (→ INVALID_REQUEST). Field type validation (method/
        # params) lives in __init__ so it covers every construction path, not
        # just this one — e.g. batch sub-commands that bypass from_json. (M-08)
        obj = json.loads(data)
        if not isinstance(obj, dict):
            raise ValueError("JSON-RPC request must be a JSON object")
        return cls(
            id=obj.get("id"),
            method=obj.get("method"),
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
        # Reject non-object JSON (e.g. "null", a bare number, an array) before
        # .get() — otherwise it raises a misleading AttributeError and the
        # caller sees an unhelpful traceback instead of a protocol error. The
        # daemon always sends an object, but a client parsing an untrusted
        # source shouldn't crash on shape. Mirrors JSONRPCRequest.from_json's
        # dict check.
        if not isinstance(obj, dict):
            raise ValueError("JSON-RPC response must be a JSON object")
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
BACKGROUND_UNAVAILABLE = -1007  # background delivery can't land (no Invoke pattern / occluded); caller retries foreground. (-1006 reserved for future SERVICE_BUSY.)
DAEMON_INTERNAL_ERROR = -1999


def make_error(code: int, message: str, id: int | str | None = None) -> JSONRPCResponse:
    return JSONRPCResponse(id=id, error={"code": code, "message": message})
