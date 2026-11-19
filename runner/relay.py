"""Counting relay for a lent proxy (2026-11-19, owner: "add tracking for traffic volumes").

The task is handed scheme://user:pass@127.0.0.1:<port> instead of the proxy itself. Each connection is passed byte for byte to the
real proxy: the task's own CONNECT / absolute-URL requests and its Proxy-Authorization go through unchanged, nothing is parsed or
decrypted. So it works for every client (requests, httpx, aiohttp, curl, Playwright) and for HTTP and SOCKS proxies alike, and the
count is what the provider sees: every byte on the proxy connection, TLS included. NO_PROXY traffic (hub, GitHub, storage) never
reaches it. An https:// proxy (TLS to the proxy itself) is not relayed: the task gets it directly, uncounted.

The worker reports the counts to the hub as deltas (take()), every REPORT_EVERY_S and once when the task ends. Nothing here may fail a
task: if the relay can't start, the task gets the real proxy as before.
"""
import socket
import threading
from urllib.parse import urlsplit, urlunsplit

REPORT_EVERY_S = 300
BUF = 65536


class CountingRelay:
    def __init__(self, upstream_host, upstream_port, connect_timeout=30):
        self.upstream = (upstream_host, int(upstream_port))
        self.connect_timeout = connect_timeout
        self.up = self.down = self.conns = 0
        self._sent = (0, 0, 0)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(256)
        self._srv.settimeout(0.5)
        self.port = self._srv.getsockname()[1]
        self._thread = threading.Thread(target=self._accept, name="proxy-relay", daemon=True)

    def start(self):
        self._thread.start()
        return self

    def _accept(self):
        while not self._stop.is_set():
            try:
                c, _ = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(c,), daemon=True).start()

    def _handle(self, c):
        try:
            u = socket.create_connection(self.upstream, timeout=self.connect_timeout)
        except OSError:
            c.close()                       # the task sees a refused proxy connection, as it would without the relay
            return
        u.settimeout(None)
        c.settimeout(None)
        with self._lock:
            self.conns += 1
        done = threading.Event()
        t = threading.Thread(target=self._pump, args=(c, u, "up", done), daemon=True)
        t.start()
        self._pump(u, c, "down", None)
        done.wait(timeout=5)
        for s in (c, u):
            try:
                s.close()
            except OSError:
                pass

    def _pump(self, src, dst, way, done):
        try:
            while True:
                b = src.recv(BUF)
                if not b:
                    break
                dst.sendall(b)
                with self._lock:
                    if way == "up":
                        self.up += len(b)
                    else:
                        self.down += len(b)
        except OSError:
            pass
        finally:
            try:
                dst.shutdown(socket.SHUT_WR)    # pass the end on, so the other direction finishes too
            except OSError:
                pass
            if done is not None:
                done.set()

    def totals(self):
        with self._lock:
            return self.up, self.down, self.conns

    def take(self):
        """Bytes up, bytes down and connections since the last take()."""
        with self._lock:
            now = (self.up, self.down, self.conns)
            d = tuple(a - b for a, b in zip(now, self._sent))
            self._sent = now
            return d

    def stop(self):
        self._stop.set()
        try:
            self._srv.close()
        except OSError:
            pass


def start_for(proxy_url):
    """(relay, url for the task) for a proxy URL, or (None, proxy_url) when it is not relayed (https:// proxy or unreadable URL)."""
    p = urlsplit(proxy_url)
    if p.scheme not in ("http", "socks5", "socks5h", "socks4") or not p.hostname or not p.port:
        return None, proxy_url
    relay = CountingRelay(p.hostname, p.port).start()
    auth = p.netloc.rsplit("@", 1)[0] + "@" if "@" in p.netloc else ""
    return relay, urlunsplit((p.scheme, f"{auth}127.0.0.1:{relay.port}", p.path, p.query, p.fragment))


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1000 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000
