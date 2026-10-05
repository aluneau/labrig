"""UDP relay from the host to the WireGuard of lab group routers

A router's uplink sits on a libvirt NAT network (e.g. 192.168.122.x): the LAN can't reach it, and
libvirt's own firewall rules (iptables or nftables backend) reject new inbound connections to NAT
networks, rules it rewrites whenever a network or the daemon restarts. Rather than fighting them as
root (DNAT + holes in libvirt's chains, re-applied after every reboot / libvirt start), the app relays
the UDP datagrams itself: the host -> guest path is OUTPUT traffic, which libvirt allows, so nothing
privileged is needed besides the host firewall letting the WG_HOST_PORTS range in (scripts/setup.sh).

One listening socket per group (host_port) and one upstream socket per client address: the router sees
each device as <host bridge address>:<ephemeral port>, and WireGuard's roaming follows it. Throughput is
what a Python loop forwards (a few hundred Mbit/s), plenty for kubectl, SSH and web consoles.
"""
import logging
import selectors
import socket
import threading
import time
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

CLIENT_IDLE = 180       # s without traffic before a client's upstream socket is closed (keepalive = 25 s)
MAX_CLIENTS = 128       # per group
MAX_DATAGRAM = 65535
Target = Tuple[str, int]


class _Listener:
    def __init__(self, port: int, sock: socket.socket, target: Target):
        self.port = port
        self.sock = sock
        self.target = target
        self.clients: Dict[Tuple[str, int], Tuple[socket.socket, float]] = {}


class UDPRelay:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._sel: Optional[selectors.BaseSelector] = None
        self._listeners: Dict[int, _Listener] = {}
        self._errors: Dict[int, str] = {}
        self._thread: Optional[threading.Thread] = None
        self._wake_r: Optional[socket.socket] = None
        self._wake_w: Optional[socket.socket] = None
        self._last_gc = 0.0

    # control (any thread)

    def apply(self, wanted: Dict[int, Target], listen: str = "0.0.0.0") -> None:
        """Make the relay forward exactly `wanted`: host UDP port -> (router address, port)"""
        with self._lock:
            for port in [p for p in self._listeners if p not in wanted]:
                self._close_listener(port)
                logger.info(f"WireGuard relay: stopped udp/{port}")
            for port in [p for p in self._errors if p not in wanted]:
                del self._errors[port]
            for port, target in wanted.items():
                current = self._listeners.get(port)
                if current is not None:
                    if current.target != target:
                        self._drop_clients(current)
                        current.target = target
                        logger.info(f"WireGuard relay: udp/{port} -> {target[0]}:{target[1]}")
                    continue
                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    sock.bind((listen, port))
                    sock.setblocking(False)
                except OSError as e:
                    sock.close()
                    self._errors[port] = f"cannot listen on udp {listen}:{port}: {e.strerror or e}"
                    logger.warning(f"WireGuard relay: {self._errors[port]}")
                    continue
                self._errors.pop(port, None)
                self._ensure_thread()
                listener = _Listener(port, sock, target)
                self._listeners[port] = listener
                self._sel.register(sock, selectors.EVENT_READ, ("listen", listener, None))
                logger.info(f"WireGuard relay: udp {listen}:{port} -> {target[0]}:{target[1]}")
            self._wake()

    def status(self, port: int) -> Tuple[bool, Optional[str]]:
        with self._lock:
            return port in self._listeners, self._errors.get(port)

    def stop(self) -> None:
        self.apply({})

    # internals

    def _ensure_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._sel = selectors.DefaultSelector()
        self._wake_r, self._wake_w = socket.socketpair()
        self._wake_r.setblocking(False)
        self._sel.register(self._wake_r, selectors.EVENT_READ, ("wake", None, None))
        self._thread = threading.Thread(target=self._run, name="wg-relay", daemon=True)
        self._thread.start()

    def _wake(self) -> None:
        if self._wake_w is not None:
            try:
                self._wake_w.send(b"x")
            except OSError:
                pass

    def _close_listener(self, port: int) -> None:
        listener = self._listeners.pop(port)
        self._drop_clients(listener)
        self._unregister(listener.sock)

    def _drop_clients(self, listener: _Listener) -> None:
        for up, _ in listener.clients.values():
            self._unregister(up)
        listener.clients.clear()

    def _unregister(self, sock: socket.socket) -> None:
        try:
            self._sel.unregister(sock)
        except (KeyError, ValueError):
            pass
        sock.close()

    def _run(self) -> None:
        while True:
            try:
                events = self._sel.select(timeout=10)
            except OSError:
                time.sleep(0.5)
                continue
            with self._lock:
                for key, _ in events:
                    kind, listener, client = key.data
                    try:
                        if kind == "wake":
                            self._wake_r.recv(4096)
                        elif kind == "listen":
                            self._from_clients(listener)
                        elif listener.port in self._listeners and client in listener.clients:
                            self._from_router(listener, client)
                    except Exception:
                        logger.exception("WireGuard relay")
                now = time.monotonic()
                if now - self._last_gc > 30:
                    self._last_gc = now
                    for listener in self._listeners.values():
                        for addr, (up, seen) in list(listener.clients.items()):
                            if now - seen > CLIENT_IDLE:
                                del listener.clients[addr]
                                self._unregister(up)

    def _from_clients(self, listener: _Listener) -> None:
        for _ in range(256):
            try:
                data, addr = listener.sock.recvfrom(MAX_DATAGRAM)
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                return
            entry = listener.clients.get(addr)
            if entry is None:
                if len(listener.clients) >= MAX_CLIENTS:  # drop the least recently seen
                    oldest = min(listener.clients, key=lambda a: listener.clients[a][1])
                    self._unregister(listener.clients.pop(oldest)[0])
                up = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                up.setblocking(False)
                try:
                    up.connect(listener.target)
                except OSError as e:
                    up.close()
                    logger.info(f"WireGuard relay udp/{listener.port}: cannot reach {listener.target}: {e}")
                    return
                self._sel.register(up, selectors.EVENT_READ, ("up", listener, addr))
                entry = (up, time.monotonic())
            listener.clients[addr] = (entry[0], time.monotonic())
            try:
                entry[0].send(data)
            except OSError:
                pass  # router down / unreachable (ICMP): WireGuard retries by itself

    def _from_router(self, listener: _Listener, addr: Tuple[str, int]) -> None:
        up, _ = listener.clients[addr]
        for _ in range(256):
            try:
                data = up.recv(MAX_DATAGRAM)
            except (BlockingIOError, InterruptedError):
                return
            except OSError:  # e.g. ECONNREFUSED from an ICMP unreachable: keep the socket
                return
            listener.clients[addr] = (up, time.monotonic())
            try:
                listener.sock.sendto(data, addr)
            except OSError:
                return


relay = UDPRelay()
