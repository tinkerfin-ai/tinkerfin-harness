"""通过父进程控制连接释放旧宿主退出，不依赖时间或信号竞速"""

import json
import socket
import sys
from pathlib import Path


def main() -> None:
    resources = Path(__file__).resolve().parents[2]
    port = json.loads((resources / "control.json").read_text())["port"]
    initial = json.loads(sys.stdin.readline())
    assert initial["configuration"]["device_id"].startswith("aaaa")
    with socket.create_connection(("127.0.0.1", port)) as control:
        print(json.dumps({"kind": "state", "state": "online"}), flush=True)
        for line in sys.stdin:
            message = json.loads(line)
            if message["kind"] == "stop":
                print(json.dumps({"kind": "state", "state": "offline"}), flush=True)
                control.sendall(b"stopping\n")
                with control.makefile("rb") as reader:
                    assert reader.readline() == b"exit\n"
                return
            if message["kind"].startswith("maintenance_"):
                state = (
                    "ready" if message["kind"] == "maintenance_prepare" else "cancelled"
                )
                print(
                    json.dumps(
                        {
                            "kind": "maintenance",
                            "request_id": message["request_id"],
                            "state": state,
                        }
                    ),
                    flush=True,
                )


if __name__ == "__main__":
    main()
