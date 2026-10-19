"""通过测试专用控制连接发送宿主事件，停止请求仍走原生标准输入协议"""

import json
import selectors
import socket
import sys
from pathlib import Path


def main() -> None:
    resources = Path(__file__).resolve().parents[2]
    port = json.loads((resources / "feedback-control.json").read_text())["port"]
    initial = json.loads(sys.stdin.readline())
    assert initial["kind"] == "initialize"
    with socket.create_connection(("127.0.0.1", port)) as control:
        with selectors.DefaultSelector() as ready:
            ready.register(sys.stdin, selectors.EVENT_READ)
            ready.register(control, selectors.EVENT_READ)
            buffer = bytearray()
            while True:
                for key, _ in ready.select():
                    if key.fileobj is sys.stdin:
                        line = sys.stdin.readline()
                        if not line:
                            return
                        command = json.loads(line)
                        if command["kind"] == "stop":
                            print(
                                json.dumps({"kind": "state", "state": "stopped"}),
                                flush=True,
                            )
                            return
                    else:
                        chunk = control.recv(4096)
                        if not chunk:
                            return
                        buffer.extend(chunk)
                        while b"\n" in buffer:
                            line, _, buffer = buffer.partition(b"\n")
                            command = json.loads(line)
                            if command.get("kind") == "exit":
                                return
                            print(json.dumps(command), flush=True)


if __name__ == "__main__":
    main()
