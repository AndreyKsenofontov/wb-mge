"""Minimal SCPI client for the Rigol DHO804 over its raw socket (port 5555).

Usage: scope.py shot <file.png> [cmd ...]   -- send commands, then save a screenshot
       scope.py q "<query>" [...]            -- run queries and print the answers
       scope.py c "<cmd>" [...]              -- send commands
"""
import os, socket, sys, time

HOST, PORT = os.environ.get("SCOPE_HOST", "192.168.13.122"), 5555


class Scope:
    def __init__(self):
        self.s = socket.create_connection((HOST, PORT), timeout=10)

    def cmd(self, c):
        self.s.sendall((c + "\n").encode())
        time.sleep(0.05)

    def query(self, q, binary=False):
        self.s.sendall((q + "\n").encode())
        if not binary:
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = self.s.recv(65536)
                if not chunk:
                    break
                buf += chunk
            return buf.decode(errors="replace").strip()
        # IEEE 488.2 definite-length block: #<n><len...><data>
        head = b""
        while len(head) < 2:
            head += self.s.recv(2 - len(head))
        assert head[0:1] == b"#", head
        n = int(head[1:2])
        lenbytes = b""
        while len(lenbytes) < n:
            lenbytes += self.s.recv(n - len(lenbytes))
        length = int(lenbytes)
        data = b""
        while len(data) < length:
            chunk = self.s.recv(min(65536, length - len(data)))
            if not chunk:
                break
            data += chunk
        try:
            self.s.settimeout(0.5)
            self.s.recv(16)  # trailing newline
        except socket.timeout:
            pass
        self.s.settimeout(10)
        return data

    def screenshot(self, path):
        data = self.query(":DISPlay:DATA? PNG", binary=True)
        open(path, "wb").write(data)
        return len(data)


if __name__ == "__main__":
    sc = Scope()
    mode = sys.argv[1]
    if mode == "shot":
        for c in sys.argv[3:]:
            sc.cmd(c)
        time.sleep(1.0)
        print("png bytes:", sc.screenshot(sys.argv[2]))
    elif mode == "q":
        for q in sys.argv[2:]:
            print(q, "->", sc.query(q))
    elif mode == "c":
        for c in sys.argv[2:]:
            sc.cmd(c)
        print("ok")
