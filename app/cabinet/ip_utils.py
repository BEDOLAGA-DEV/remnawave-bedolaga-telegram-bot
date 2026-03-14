"""Shared IP extraction utilities for cabinet module."""

from ipaddress import ip_address, ip_network

from fastapi import HTTPException, Request, status

from app.config import settings


def _is_trusted_proxy(peer_ip: str, trusted: set[str]) -> bool:
    """Check if peer IP matches any trusted proxy entry (IP or CIDR)."""
    if not trusted:
        return False
    try:
        addr = ip_address(peer_ip)
    except ValueError:
        return False
    for entry in trusted:
        try:
            if '/' in entry:
                if addr in ip_network(entry, strict=False):
                    return True
            elif addr == ip_address(entry):
                return True
        except ValueError:
            continue
    return False


def get_client_ip(request: Request) -> str:
    """Extract real client IP, trusting proxy headers only from known proxies.

    Raises HTTPException 400 if the peer IP cannot be determined
    (request.client is None — e.g., test harness or broken transport).
    """
    if not request.client:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Unable to determine client IP',
        )
    peer_ip = request.client.host
    trusted_proxies = settings.get_cabinet_trusted_proxies()

    if trusted_proxies and _is_trusted_proxy(peer_ip, trusted_proxies):
        forwarded = request.headers.get('X-Forwarded-For', '').split(',')[0].strip()
        if forwarded:
            try:
                ip_address(forwarded)
                return forwarded
            except ValueError:
                pass  # invalid IP in header — fall through to peer_ip
        real_ip = request.headers.get('X-Real-IP', '').strip()
        if real_ip:
            try:
                ip_address(real_ip)
                return real_ip
            except ValueError:
                pass

    return peer_ip
