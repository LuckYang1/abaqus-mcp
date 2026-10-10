import json
import os
import socket
from typing import Any

DEFAULT_MAX_MESSAGE_BYTES = 16 * 1024 * 1024


class ProtocolError(RuntimeError):
    pass


def max_message_bytes() -> int:
    return int(os.environ.get("ABAQUS_MCP_MAX_MESSAGE_BYTES", str(DEFAULT_MAX_MESSAGE_BYTES)))


def send_message(sock: socket.socket, payload: dict[str, Any]) -> None:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    sock.sendall(data + b"\n")


def read_message(sock: socket.socket, max_bytes: int) -> dict[str, Any]:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            raise ProtocolError("socket closed before a complete message was received")
        newline = chunk.find(b"\n")
        if newline >= 0:
            total += newline
            if total > max_bytes:
                raise ProtocolError(f"message exceeded {max_bytes} bytes")
            chunks.append(chunk[:newline])
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > max_bytes:
            raise ProtocolError(f"message exceeded {max_bytes} bytes")
    message = json.loads(b"".join(chunks).decode("utf-8"))
    if not isinstance(message, dict):
        raise ProtocolError("protocol message must be a JSON object")
    return message
