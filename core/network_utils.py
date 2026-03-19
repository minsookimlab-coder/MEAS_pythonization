"""
Network and VISA address utility functions.
"""
import subprocess
import socket
from typing import Optional


def build_visa_string(interface_type: str, address: str, port: Optional[int] = None) -> str:
    """
    interface_type, address, port 로부터 PyVISA 리소스 문자열을 생성합니다.
    인스턴스 생성 없이 VISA 주소를 미리 확인할 때 사용합니다.
    """
    if interface_type == "LAN":
        if port and port != 0:
            return f"TCPIP0::{address}::{port}::SOCKET"
        else:
            return f"TCPIP0::{address}::inst0::INSTR"
    elif interface_type == "GPIB":
        return f"GPIB0::{address}::INSTR"
    elif interface_type in ("RS232", "USB"):
        return address
    else:
        raise ValueError(f"Unknown Interface Type: {interface_type}")


def _get_arp_table() -> str:
    """시스템 ARP 테이블 전체를 문자열로 반환합니다."""
    try:
        return subprocess.check_output('arp -a', shell=True).decode('cp949', errors='ignore')
    except Exception:
        return ""


def find_ip_for_mac(mac_address: str) -> str | None:
    """ARP 테이블에서 MAC 주소에 해당하는 IP를 반환합니다. 못 찾으면 None."""
    mac_to_find = mac_address.replace(':', '-').lower()
    for line in _get_arp_table().split('\n'):
        if mac_to_find in line.lower():
            parts = line.split()
            if len(parts) >= 2:
                return parts[0]
    return None


def find_mac_for_ip(ip: str) -> str | None:
    """ARP 테이블에서 IP에 해당하는 MAC 주소를 반환합니다. 못 찾으면 None."""
    for line in _get_arp_table().split('\n'):
        parts = line.split()
        if len(parts) >= 2 and parts[0] == ip:
            return parts[1].upper()
    return None


def resolve_address(interface_type: str, address: str, mac_address: str = "") -> str:
    """
    DHCP 환경에 대비하여 실제 IP 주소를 추적·반환합니다.

    우선순위:
    1. MAC 주소가 있으면 ARP 테이블로 현재 IP 추적
    2. 입력값이 MAC 포맷이면 동일하게 ARP로 추적
    3. 일반 IPv4 형태면 그대로 반환
    4. 호스트명/도메인이면 DNS 조회
    5. 모두 실패하면 원본 입력값 반환
    """
    if interface_type != "LAN":
        return address

    # 1 & 2. MAC 주소 기반 IP 추적
    mac_to_find = ""
    if mac_address and len(mac_address) >= 11:
        mac_to_find = mac_address.replace(':', '-').lower()
    elif (':' in address or '-' in address) and len(address) >= 11:
        mac_to_find = address.replace(':', '-').lower()

    if mac_to_find:
        resolved = find_ip_for_mac(mac_to_find)
        if resolved:
            print(f"[Auto-IP-Resolver] MAC '{mac_to_find}' resolved to dynamic IP: {resolved}")
            return resolved

    # 3. 일반 IPv4 형태면 그대로 통과
    if address.count('.') == 3 and all(p.isdigit() for p in address.split('.')):
        return address

    # 4. 호스트명이면 DNS 조회
    try:
        resolved = socket.gethostbyname(address)
        print(f"[Auto-IP-Resolver] Hostname '{address}' resolved to IP: {resolved}")
        return resolved
    except socket.gaierror:
        pass

    return address
