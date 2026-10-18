"""보안 도우미: 메일 서버 접속 대상 검사, 인증서를 확인하는 TLS, 요청 횟수 제한, 실제 접속 IP.

- 메일 서버 주소는 사용자가 넣는다(학교·회사 메일). 그대로 접속하면 서버 내부망(127.0.0.1, 169.254.169.254 등)을
  두드리는 데 쓰일 수 있으므로, 주소를 미리 풀어 공인 IP일 때만 그 IP로 접속한다 (풀고 나서 바뀌는 DNS에도 안전하게)
- 인증서는 반드시 확인한다. 확인하지 않으면 중간에 끼어든 사람이 메일 비밀번호를 그대로 받는다
"""

import imaplib
import ipaddress
import socket
import ssl
import threading
import time
from typing import Dict, List, Optional, Tuple

from fastapi import Request

# 메일 서버 포트: 처음부터 TLS로 붙는 IMAPS만 쓴다
ALLOWED_IMAP_PORTS = frozenset({993})


class UnsafeHost(Exception):
    """접속하면 안 되는 메일 서버 주소·포트."""


def tls_context() -> ssl.SSLContext:
    """인증서와 호스트 이름을 확인하는 TLS 1.2 이상 설정."""
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def _is_public(ip: str) -> bool:
    addr = ipaddress.ip_address(ip.split("%", 1)[0])
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return addr.is_global and not addr.is_multicast


def resolve_public(host: str, port: int) -> List[str]:
    """주소를 IP로 풀어, 모든 결과가 공인 IP일 때만 그 IP들을 돌려준다. 이름을 못 풀면 socket.gaierror(OSError)."""
    if port not in ALLOWED_IMAP_PORTS:
        raise UnsafeHost(f"port {port} not allowed")
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    ips = [info[4][0] for info in infos]
    if not ips or not all(_is_public(ip) for ip in ips):
        raise UnsafeHost(f"{host} resolves to a non-public address")
    return list(dict.fromkeys(ips))


class PinnedIMAP4_SSL(imaplib.IMAP4_SSL):
    """미리 검사한 IP로만 접속하고, 인증서는 원래 주소(host) 이름으로 확인한다."""

    def __init__(self, host: str, ips: List[str], port: int, timeout: float):
        self._pinned_ips = ips  # 부모 __init__이 바로 접속하므로 먼저 둔다
        super().__init__(host, port, ssl_context=tls_context(), timeout=timeout)

    def _create_socket(self, timeout):
        # IPv6가 안 되는 서버도 있으니 검사한 IP를 차례로 시도한다 (socket.create_connection이 하던 대로)
        error: Optional[OSError] = None
        for ip in self._pinned_ips:
            try:
                sock = socket.create_connection((ip, self.port), timeout)
            except OSError as e:
                error = e
                continue
            return self.ssl_context.wrap_socket(sock, server_hostname=self.host)
        raise error or OSError(f"no address for {self.host}")


class RateLimiter:
    """키(IP·계정)마다 window초 동안 limit번까지. 서버 메모리에만 둔다 (프로세스 하나만 띄운다)."""

    def __init__(self, limit: int, window: float):
        self.limit, self.window = limit, window
        self._buckets: Dict[str, Tuple[int, float]] = {}
        self._lock = threading.Lock()

    def hit(self, key: str) -> bool:
        """한 번 쓴다. 한도를 넘었으면 False (넘은 시도는 세지 않는다)."""
        now = time.monotonic()
        with self._lock:
            count, reset_at = self._buckets.get(key, (0, now + self.window))
            if now >= reset_at:
                count, reset_at = 0, now + self.window
            if count >= self.limit:
                return False
            self._buckets[key] = (count + 1, reset_at)
            if len(self._buckets) >= 5000:
                for k, (_, r) in list(self._buckets.items()):
                    if r <= now:
                        del self._buckets[k]
            return True

    def clear(self) -> None:
        with self._lock:
            self._buckets.clear()


def client_ip(request: Request) -> str:
    """실제 접속 IP. 앞단 프록시(Railway)가 X-Forwarded-For 맨 끝에 붙인 값을 쓴다.
    맨 앞 값은 사용자가 마음대로 넣을 수 있어서 쓰지 않는다."""
    forwarded = request.headers.get("x-forwarded-for", "")
    last: Optional[str] = forwarded.rsplit(",", 1)[-1].strip() if forwarded else None
    if last:
        return last
    return request.client.host if request.client else "unknown"
