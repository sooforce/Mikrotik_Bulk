#!/usr/bin/env python3
"""
MikroTik Bulk Provisioner
=========================
Windows desktop tool for bulk provisioning MikroTik routers in an ISP staging lab.

Workflow
--------
1. Scan a DHCP-assigned IP range for routers (TCP port 22 check).
2. SSH into each responding IP to gather identity, version, and MAC address.
3. Operator selects routers from the table and clicks "Provision Selected".
4. For each selected router the tool runs via SSH:
       /tool fetch url="<CONFIG_URL>" mode=http dst-path=full-config.rsc
       /import file-name=full-config.rsc

Dependencies: PyQt6, paramiko
"""

import sys
import os
import socket
import threading
import logging
import ipaddress
import datetime
import time
import struct
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Optional, List, Dict

import subprocess
import urllib.request
import urllib.error

import paramiko
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QTableWidget, QTableWidgetItem, QHeaderView, QPushButton, QLabel,
    QLineEdit, QSpinBox, QGroupBox, QPlainTextEdit, QSplitter,
    QAbstractItemView, QMessageBox,
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QIcon


# ─────────────────────────────────────────────────────────────────────────────
#  Constants & Defaults
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_IP_START    = "192.168.88.100"
DEFAULT_IP_END      = "192.168.88.254"
DEFAULT_SSH_USER    = "admin"
DEFAULT_SSH_PASS    = ""
DEFAULT_CONFIG_URL  = "http://192.168.88.5:80/mikrotik-provision/full-config.rsc"
DEFAULT_CONCURRENCY = 6

DEFAULT_DHCP_SERVER_IP  = "192.168.88.1"
DEFAULT_DHCP_POOL_START = "192.168.88.100"
DEFAULT_DHCP_POOL_END   = "192.168.88.200"
DEFAULT_DHCP_SUBNET     = "255.255.255.0"
DEFAULT_DHCP_GATEWAY    = "192.168.88.1"
DEFAULT_DHCP_LEASE      = 3600          # seconds

SSH_PORT           = 22
SSH_TIMEOUT        = 10     # seconds – TCP connect + SSH handshake
SSH_BANNER_TIMEOUT = 15     # seconds – waiting for SSH banner
CMD_TIMEOUT        = 30     # seconds – individual SSH command
SSH_RETRIES        = 3      # total SSH connect attempts
FETCH_RETRIES      = 2      # total /tool fetch attempts
IMPORT_RETRIES     = 2      # total /import attempts
FETCH_TIMEOUT      = 120    # seconds – /tool fetch may take a while

SCAN_THREADS = 60           # parallel TCP-check workers during discovery
SCAN_TIMEOUT = 1.5          # seconds – TCP port-22 probe timeout

LOG_FILE = "mikrotik_provisioner.log"

# Table column indices
COL_CHECK    = 0
COL_IP       = 1
COL_MAC      = 2
COL_IDENTITY = 3
COL_VERSION  = 4
COL_STATUS   = 5

# Row background / foreground colours keyed by status string
STATUS_COLORS: Dict[str, str] = {
    "Discovered":       "#dbeafe",  # blue-50
    "Queued":           "#fef9c3",  # yellow-100
    "Connecting":       "#fef3c7",  # amber-100
    "Fetching config":  "#ffedd5",  # orange-100
    "Importing config": "#fce7f3",  # pink-100
    "Success":          "#dcfce7",  # green-100
    "Failed":           "#fee2e2",  # red-100
}

STATUS_FG: Dict[str, str] = {
    "Discovered":       "#1e40af",  # blue-800
    "Queued":           "#713f12",  # yellow-900
    "Connecting":       "#92400e",  # amber-800
    "Fetching config":  "#7c2d12",  # orange-900
    "Importing config": "#831843",  # pink-900
    "Success":          "#14532d",  # green-900
    "Failed":           "#7f1d1d",  # red-900
}


# ─────────────────────────────────────────────────────────────────────────────
#  Logging – writes to file AND the in-app log panel (via signal)
# ─────────────────────────────────────────────────────────────────────────────

def _setup_file_logger() -> logging.Logger:
    logger = logging.getLogger("mikrotik_provisioner")
    logger.setLevel(logging.DEBUG)
    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)-7s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logger.addHandler(fh)
    return logger

log = _setup_file_logger()

# Suppress console window on Windows when spawning subprocesses
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ─────────────────────────────────────────────────────────────────────────────
#  ARP table helper
# ─────────────────────────────────────────────────────────────────────────────

def _get_arp_table() -> Dict[str, str]:
    """
    Parse the Windows ARP cache and return {ip: MAC} mapping.
    MAC addresses are normalised to uppercase colon-separated notation.
    Returns an empty dict on any error.
    """
    arp_map: Dict[str, str] = {}
    try:
        output = subprocess.check_output(
            ["arp", "-a"],
            timeout=5,
            creationflags=_NO_WINDOW,
        ).decode("utf-8", errors="replace")
        for line in output.splitlines():
            parts = line.split()
            # Windows: "  192.168.88.100   aa-bb-cc-dd-ee-ff   dynamic"
            if len(parts) >= 2:
                ip_part  = parts[0].strip()
                mac_part = parts[1].strip()
                if (
                    ip_part.count(".") == 3
                    and "-" in mac_part
                    and len(mac_part) == 17
                ):
                    arp_map[ip_part] = mac_part.upper().replace("-", ":")
    except Exception as exc:
        log.debug("ARP table lookup failed: %s", exc)
    return arp_map


# ─────────────────────────────────────────────────────────────────────────────
#  Data model
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class RouterInfo:
    """Mutable state for a single discovered router."""
    ip:       str
    mac:      str = "—"
    identity: str = "—"
    version:  str = "—"
    status:   str = "Discovered"
    error:    str = ""
    row:      int = -1   # table row assigned after insertion


# ─────────────────────────────────────────────────────────────────────────────
#  SSH helpers  (blocking – always call from a worker thread)
# ─────────────────────────────────────────────────────────────────────────────

def _ssh_connect(ip: str, username: str, password: str) -> paramiko.SSHClient:
    """Open an SSH session to *ip*, retrying up to SSH_RETRIES times."""
    last_exc: Exception = RuntimeError("no attempts made")
    for attempt in range(1, SSH_RETRIES + 1):
        try:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(
                hostname=ip,
                port=SSH_PORT,
                username=username,
                password=password,
                timeout=SSH_TIMEOUT,
                banner_timeout=SSH_BANNER_TIMEOUT,
                allow_agent=False,
                look_for_keys=False,
            )
            return client
        except Exception as exc:
            last_exc = exc
            log.debug("[%s] SSH connect attempt %d/%d failed: %s",
                      ip, attempt, SSH_RETRIES, exc)
            if attempt < SSH_RETRIES:
                time.sleep(1.5)
    raise last_exc


def _ssh_cmd(client: paramiko.SSHClient, command: str,
             timeout: int = CMD_TIMEOUT) -> str:
    """
    Execute *command* via a direct channel with a hard deadline.
    Polls recv_ready / exit_status_ready so a hung router cannot block
    the worker thread indefinitely.
    Raises TimeoutError when *timeout* seconds elapse without completion.
    """
    transport = client.get_transport()
    channel   = transport.open_session()
    try:
        channel.settimeout(timeout)
        channel.exec_command(command)
        stdout_buf: bytes = b""
        stderr_buf: bytes = b""
        deadline = time.monotonic() + timeout
        while True:
            if time.monotonic() > deadline:
                raise TimeoutError(
                    f"Command timed out after {timeout}s: {command!r}")
            if channel.recv_ready():
                stdout_buf += channel.recv(65536)
            if channel.recv_stderr_ready():
                stderr_buf += channel.recv_stderr(65536)
            if channel.exit_status_ready() and not channel.recv_ready():
                # Final drain of any buffered stderr
                while channel.recv_stderr_ready():
                    stderr_buf += channel.recv_stderr(65536)
                break
            time.sleep(0.05)
    finally:
        channel.close()
    out = stdout_buf.decode("utf-8", errors="replace").strip()
    err = stderr_buf.decode("utf-8", errors="replace").strip()
    if err:
        log.debug("SSH stderr for %r: %s", command, err)
    return out


def _gather_router_info(ip: str, username: str, password: str) -> dict:
    """
    SSH into *ip* and retrieve identity, RouterOS version, and ether1 MAC.
    Returns a dict with keys 'identity', 'version', 'mac'.
    Connection is closed before returning.
    """
    info = {"identity": "—", "version": "—", "mac": "—"}
    client = _ssh_connect(ip, username, password)
    try:
        # :put returns a bare value, easier to parse than the full print output
        for key, cmd in (
            ("identity", ":put [/system identity get name]"),
            ("version",  ":put [/system resource get version]"),
            ("mac",      ":put [/interface ethernet get [find name=ether1] mac-address]"),
        ):
            try:
                result = _ssh_cmd(client, cmd, timeout=8).splitlines()
                if result:
                    info[key] = result[0].strip()
            except Exception as exc:
                log.debug("[%s] %s fetch failed: %s", ip, key, exc)
    finally:
        client.close()
    return info


# ─────────────────────────────────────────────────────────────────────────────
#  HFS URL Check Worker
# ─────────────────────────────────────────────────────────────────────────────

class UrlCheckWorker(QThread):
    """
    Sends an HTTP HEAD request to the config URL and reports whether the file
    is reachable from this laptop.  Runs in a background thread so the GUI
    stays responsive.
    """

    result = pyqtSignal(bool, str)   # (success, message)

    def __init__(self, url: str, parent=None):
        super().__init__(parent)
        self.url = url

    def run(self):
        try:
            req = urllib.request.Request(self.url, method="HEAD")
            with urllib.request.urlopen(req, timeout=10) as resp:
                code = resp.status
                if code == 200:
                    self.result.emit(
                        True, f"OK (HTTP {code}) — config file is reachable.")
                else:
                    self.result.emit(
                        False, f"HTTP {code} — unexpected status code.")
        except urllib.error.HTTPError as exc:
            self.result.emit(False, f"HTTP error {exc.code}: {exc.reason}")
        except urllib.error.URLError as exc:
            self.result.emit(False, f"Cannot reach URL: {exc.reason}")
        except Exception as exc:
            self.result.emit(False, f"Error: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
#  Scan Worker
# ─────────────────────────────────────────────────────────────────────────────

class ScanWorker(QThread):
    """
    Probes every IP in the configured range (TCP port 22).
    For responsive IPs it SSH-authenticates to pull identity / version / MAC.
    Results are delivered to the GUI through Qt signals.
    """

    router_found  = pyqtSignal(object)   # RouterInfo instance
    progress      = pyqtSignal(int, int) # (completed_count, total_count)
    scan_finished = pyqtSignal(int)      # total routers found
    log_message   = pyqtSignal(str)

    def __init__(self, ip_start: str, ip_end: str,
                 ssh_user: str, ssh_pass: str, parent=None):
        super().__init__(parent)
        self.ip_start = ip_start
        self.ip_end   = ip_end
        self.ssh_user = ssh_user
        self.ssh_pass = ssh_pass
        self._stop_event = threading.Event()

    def stop(self):
        """Request a graceful stop before the scan completes."""
        self._stop_event.set()

    def _tcp_open(self, ip: str) -> bool:
        """Return True when SSH port on *ip* accepts a TCP connection."""
        try:
            with socket.create_connection((ip, SSH_PORT), timeout=SCAN_TIMEOUT):
                return True
        except OSError:
            return False

    def _probe(self, ip: str) -> Optional[RouterInfo]:
        """Check one IP; return a populated RouterInfo or None."""
        if self._stop_event.is_set():
            return None
        if not self._tcp_open(ip):
            return None

        router = RouterInfo(ip=ip)
        try:
            details = _gather_router_info(ip, self.ssh_user, self.ssh_pass)
            router.identity = details["identity"]
            router.version  = details["version"]
            router.mac      = details["mac"]
        except Exception as exc:
            # SSH port was open but credentials failed or info unavailable
            log.warning("[%s] Could not gather details: %s", ip, exc)
            router.identity = "(auth failed?)"
        return router

    def run(self):
        try:
            start = ipaddress.IPv4Address(self.ip_start)
            end   = ipaddress.IPv4Address(self.ip_end)
        except ValueError as exc:
            self.log_message.emit(f"Invalid IP range: {exc}")
            self.scan_finished.emit(0)
            return

        ips   = [str(ipaddress.IPv4Address(n))
                 for n in range(int(start), int(end) + 1)]
        total = len(ips)
        self.log_message.emit(
            f"Scanning {total} address(es) from {self.ip_start} to {self.ip_end}…"
        )

        completed = 0
        found     = 0

        with ThreadPoolExecutor(max_workers=SCAN_THREADS) as pool:
            futures = {pool.submit(self._probe, ip): ip for ip in ips}
            for future in as_completed(futures):
                if self._stop_event.is_set():
                    break
                completed += 1
                self.progress.emit(completed, total)
                result = future.result()
                if result is not None:
                    found += 1
                    self.router_found.emit(result)
                    self.log_message.emit(
                        f"Found {result.ip}  identity={result.identity}"
                        f"  version={result.version}"
                    )

        self.scan_finished.emit(found)


# ─────────────────────────────────────────────────────────────────────────────
#  Provision Worker
# ─────────────────────────────────────────────────────────────────────────────

class ProvisionWorker(QThread):
    """
    Provisions a single router:
      1. Connect via SSH.
      2. Run /tool fetch to download the config.
      3. Run /import to apply it.

    A threading.Semaphore limits how many workers run simultaneously.
    """

    status_update = pyqtSignal(str, str, str)  # (ip, status_text, error_text)
    log_message   = pyqtSignal(str)

    def __init__(self, router: RouterInfo, ssh_user: str, ssh_pass: str,
                 config_url: str, semaphore: threading.Semaphore, parent=None):
        super().__init__(parent)
        self.router     = router
        self.ssh_user   = ssh_user
        self.ssh_pass   = ssh_pass
        self.config_url = config_url
        self.semaphore  = semaphore

    def _set_status(self, status: str, error: str = ""):
        self.status_update.emit(self.router.ip, status, error)

    def run(self):
        ip = self.router.ip

        # Acquire the semaphore slot; blocks until a slot is free
        with self.semaphore:
            # ── Step 1: SSH connect (with retries) ──────────────────────────
            self._set_status("Connecting")
            self.log_message.emit(
                f"[{ip}] Connecting via SSH (up to {SSH_RETRIES} attempts)…")
            try:
                client = _ssh_connect(ip, self.ssh_user, self.ssh_pass)
            except Exception as exc:
                msg = f"SSH connection failed after {SSH_RETRIES} attempts: {exc}"
                log.error("[%s] %s", ip, msg)
                self.log_message.emit(f"[{ip}] ERROR: {msg}")
                self._set_status("Failed", msg)
                return

            try:
                # ── Step 2: Fetch config (with retries) ──────────────────────
                self._set_status("Fetching config")
                fetch_cmd = (
                    f'/tool fetch url="{self.config_url}"'
                    f' mode=http dst-path=full-config.rsc'
                )
                fetch_error: Optional[Exception] = None
                for attempt in range(1, FETCH_RETRIES + 1):
                    try:
                        self.log_message.emit(
                            f"[{ip}] Fetch attempt {attempt}/{FETCH_RETRIES}: "
                            f"{fetch_cmd}"
                        )
                        out = _ssh_cmd(client, fetch_cmd, timeout=FETCH_TIMEOUT)
                        log.info("[%s] fetch → %s", ip, out)
                        self.log_message.emit(f"[{ip}] fetch output: {out}")
                        fetch_error = None
                        break
                    except Exception as exc:
                        fetch_error = exc
                        log.warning("[%s] Fetch attempt %d/%d failed: %s",
                                    ip, attempt, FETCH_RETRIES, exc)
                        if attempt < FETCH_RETRIES:
                            time.sleep(2)
                if fetch_error is not None:
                    raise RuntimeError(
                        f"Fetch failed after {FETCH_RETRIES} attempts: "
                        f"{fetch_error}"
                    )

                # ── Step 3: Import config (with retries) ─────────────────────
                self._set_status("Importing config")
                import_cmd = "/import file-name=full-config.rsc"
                import_error: Optional[Exception] = None
                for attempt in range(1, IMPORT_RETRIES + 1):
                    try:
                        self.log_message.emit(
                            f"[{ip}] Import attempt {attempt}/{IMPORT_RETRIES}: "
                            f"{import_cmd}"
                        )
                        out = _ssh_cmd(client, import_cmd, timeout=FETCH_TIMEOUT)
                        log.info("[%s] import → %s", ip, out)
                        self.log_message.emit(f"[{ip}] import output: {out}")
                        import_error = None
                        break
                    except Exception as exc:
                        # Connection drop during import is expected — RouterOS
                        # resets interfaces / services as the config is applied.
                        exc_str = str(exc).lower()
                        if any(k in exc_str for k in
                               ("reset", "closed", "eof", "timed out")):
                            log.info(
                                "[%s] Connection ended during import (expected): %s",
                                ip, exc)
                            self.log_message.emit(
                                f"[{ip}] Note: SSH closed during import "
                                f"(normal — config may restart services)"
                            )
                            import_error = None
                            break
                        import_error = exc
                        log.warning("[%s] Import attempt %d/%d failed: %s",
                                    ip, attempt, IMPORT_RETRIES, exc)
                        if attempt < IMPORT_RETRIES:
                            time.sleep(2)
                if import_error is not None:
                    raise RuntimeError(
                        f"Import failed after {IMPORT_RETRIES} attempts: "
                        f"{import_error}"
                    )

                self._set_status("Success")
                log.info("[%s] Provisioning complete.", ip)
                self.log_message.emit(f"[{ip}] Provisioning complete.")

            except Exception as exc:
                msg = str(exc)
                log.error("[%s] Provisioning failed: %s", ip, msg)
                self.log_message.emit(f"[{ip}] FAILED: {msg}")
                self._set_status("Failed", msg)

            finally:
                try:
                    client.close()
                except Exception:
                    pass


# ─────────────────────────────────────────────────────────────────────────────
#  DHCP packet helpers  (RFC 2131)
# ─────────────────────────────────────────────────────────────────────────────

DHCP_MAGIC_COOKIE = b'\x63\x82\x53\x63'


def _parse_dhcp(data: bytes) -> dict:
    """
    Parse a raw DHCP/BOOTP packet.
    Returns a dict with fixed-header fields, an 'options' sub-dict, and a
    convenience 'msg_type' int.  Returns an empty dict on any parse failure.
    """
    if len(data) < 240:
        return {}
    if data[236:240] != DHCP_MAGIC_COOKIE:
        return {}
    pkt: dict = {}
    pkt['op']     = data[0]
    pkt['htype']  = data[1]
    pkt['hlen']   = min(data[2], 16)   # guard against corrupt values
    pkt['xid']    = data[4:8]
    pkt['flags']  = struct.unpack('!H', data[10:12])[0]
    pkt['ciaddr'] = data[12:16]
    pkt['chaddr'] = data[28: 28 + pkt['hlen']]

    # Parse TLV options (type–length–value)
    options: dict = {}
    i = 240
    while i < len(data):
        opt = data[i]
        if opt == 255:           # END
            break
        if opt == 0:             # PAD
            i += 1
            continue
        if i + 1 >= len(data):
            break
        length = data[i + 1]
        if i + 2 + length > len(data):
            break
        options[opt] = data[i + 2: i + 2 + length]
        i += 2 + length

    pkt['options']  = options
    pkt['msg_type'] = options.get(53, b'\x00')[0]
    return pkt


def _build_dhcp_reply(pkt: dict, msg_type: int, server_ip: str,
                      offered_ip: str, subnet: str, gateway: str,
                      lease_time: int) -> bytes:
    """Return a DHCP OFFER or ACK packet as raw bytes."""
    hdr = bytearray(236)
    hdr[0]           = 2                               # op = BOOTREPLY
    hdr[1]           = pkt['htype']
    hdr[2]           = pkt['hlen']
    hdr[4:8]         = pkt['xid']
    struct.pack_into('!H', hdr, 10, 0x8000)            # broadcast flag
    hdr[12:16]       = pkt['ciaddr']                   # ciaddr (0 in DISCOVER)
    hdr[16:20]       = socket.inet_aton(offered_ip)    # yiaddr
    hdr[20:24]       = socket.inet_aton(server_ip)     # siaddr
    hdr[28: 28 + pkt['hlen']] = pkt['chaddr']          # chaddr

    opts = bytearray()

    def _o(code: int, value: bytes):
        opts.append(code)
        opts.append(len(value))
        opts.extend(value)

    _o(53, bytes([msg_type]))                           # DHCP Message Type
    _o(54, socket.inet_aton(server_ip))                 # Server Identifier
    _o(51, struct.pack('!I', lease_time))               # IP Address Lease Time
    _o(58, struct.pack('!I', lease_time // 2))          # Renewal Time (T1)
    _o(59, struct.pack('!I', int(lease_time * 0.875)))  # Rebinding Time (T2)
    _o(1,  socket.inet_aton(subnet))                    # Subnet Mask
    _o(3,  socket.inet_aton(gateway))                   # Router (default gateway)
    _o(6,  socket.inet_aton(gateway))                   # DNS (fallback to gateway)
    opts.append(255)                                    # END

    return bytes(hdr) + DHCP_MAGIC_COOKIE + bytes(opts)


def _build_dhcp_nak(pkt: dict, server_ip: str) -> bytes:
    """Return a DHCP NAK packet as raw bytes."""
    hdr = bytearray(236)
    hdr[0] = 2
    hdr[1] = pkt['htype']
    hdr[2] = pkt['hlen']
    hdr[4:8] = pkt['xid']
    struct.pack_into('!H', hdr, 10, 0x8000)
    hdr[28: 28 + pkt['hlen']] = pkt['chaddr']

    opts = bytearray()
    opts += bytes([53, 1, 6])                           # DHCP NAK
    opts += bytes([54, 4]) + socket.inet_aton(server_ip)
    opts.append(255)                                    # END

    return bytes(hdr) + DHCP_MAGIC_COOKIE + bytes(opts)


# ─────────────────────────────────────────────────────────────────────────────
#  DHCP Server Worker
# ─────────────────────────────────────────────────────────────────────────────

class DhcpServerWorker(QThread):
    """
    Minimal RFC-2131 DHCP server.

    Emits device_leased(ip, mac) the instant a DHCP ACK is sent so the GUI
    can SSH-probe the device immediately — no polling or scanning required.

    Requirements:
      * The application must be run as Administrator (port 67 is privileged).
      * Disable tftpd64's own DHCP server before starting this one.
        tftpd64's TFTP server (port 69) can still run normally.
    """

    DISCOVER = 1
    OFFER    = 2
    REQUEST  = 3
    ACK      = 5
    NAK      = 6

    device_leased  = pyqtSignal(str, str)  # (ip, mac_str)
    log_message    = pyqtSignal(str)
    server_stopped = pyqtSignal()

    def __init__(self, server_ip: str, pool_start: str, pool_end: str,
                 subnet: str, gateway: str, lease_seconds: int, parent=None):
        super().__init__(parent)
        self.server_ip     = server_ip
        self.pool_start    = pool_start
        self.pool_end      = pool_end
        self.subnet        = subnet
        self.gateway       = gateway
        self.lease_seconds = lease_seconds
        self._stop_event   = threading.Event()
        self._leases:      Dict[str, tuple] = {}  # mac → (ip, expiry)
        self._pool:        List[str]        = []

    def stop(self):
        self._stop_event.set()

    def _build_pool(self):
        start = int(ipaddress.IPv4Address(self.pool_start))
        end   = int(ipaddress.IPv4Address(self.pool_end))
        self._pool = [str(ipaddress.IPv4Address(n))
                      for n in range(start, end + 1)]

    def _mac_str(self, chaddr: bytes, hlen: int) -> str:
        return ':'.join(f'{b:02X}' for b in chaddr[:hlen])

    def _assign_ip(self, mac: str) -> Optional[str]:
        """Return the current or a new IP for *mac*, or None if pool is full."""
        now = time.time()
        # Honour an existing unexpired lease (also renews it)
        if mac in self._leases:
            ip, expiry = self._leases[mac]
            if expiry > now:
                self._leases[mac] = (ip, now + self.lease_seconds)
                return ip
        # Reclaim any expired leases
        for m in [m for m, (_, exp) in list(self._leases.items())
                  if exp <= now]:
            del self._leases[m]
        # Allocate first free IP
        used = {ip for ip, _ in self._leases.values()}
        for ip in self._pool:
            if ip not in used:
                self._leases[mac] = (ip, now + self.lease_seconds)
                return ip
        return None   # pool exhausted

    def run(self):
        self._build_pool()
        try:
            srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            srv.bind(('', 67))
            srv.settimeout(1.0)
        except PermissionError:
            self.log_message.emit(
                "DHCP ERROR: Cannot bind to port 67 — "
                "restart the application as Administrator.")
            self.server_stopped.emit()
            return
        except Exception as exc:
            self.log_message.emit(f"DHCP ERROR: Failed to start server: {exc}")
            self.server_stopped.emit()
            return

        self.log_message.emit(
            f"DHCP server listening | server {self.server_ip} | "
            f"pool {self.pool_start}–{self.pool_end} | "
            f"mask {self.subnet} | gw {self.gateway}"
        )

        while not self._stop_event.is_set():
            try:
                data, _ = srv.recvfrom(4096)
            except socket.timeout:
                continue
            except Exception as exc:
                if not self._stop_event.is_set():
                    self.log_message.emit(f"DHCP recv error: {exc}")
                break
            try:
                self._handle(srv, data)
            except Exception as exc:
                self.log_message.emit(f"DHCP packet handling error: {exc}")

        try:
            srv.close()
        except Exception:
            pass
        self.log_message.emit("DHCP server stopped.")
        self.server_stopped.emit()

    def _handle(self, srv: socket.socket, data: bytes):
        pkt = _parse_dhcp(data)
        if not pkt or pkt.get('op') != 1:   # only BOOTREQUEST packets
            return

        mac = self._mac_str(pkt['chaddr'], pkt['hlen'])
        mt  = pkt['msg_type']

        # Option 54 = Server Identifier — ignore requests aimed at another server
        sid_opt = pkt['options'].get(54)
        if sid_opt and len(sid_opt) == 4:
            if socket.inet_ntoa(sid_opt) != self.server_ip:
                return

        if mt == self.DISCOVER:
            ip = self._assign_ip(mac)
            if ip is None:
                self.log_message.emit(
                    f"DHCP DISCOVER from {mac} — pool exhausted, ignoring.")
                return
            self.log_message.emit(f"DHCP DISCOVER from {mac} → OFFER {ip}")
            reply = _build_dhcp_reply(
                pkt, self.OFFER, self.server_ip, ip,
                self.subnet, self.gateway, self.lease_seconds,
            )
            srv.sendto(reply, ('255.255.255.255', 68))

        elif mt == self.REQUEST:
            # Option 50 = Requested IP Address (present in SELECTING state)
            req_opt = pkt['options'].get(50)
            requested = socket.inet_ntoa(req_opt) if (
                req_opt and len(req_opt) == 4) else None

            assigned = self._assign_ip(mac)
            if assigned and (requested is None or requested == assigned):
                self.log_message.emit(
                    f"DHCP REQUEST from {mac} → ACK {assigned}")
                reply = _build_dhcp_reply(
                    pkt, self.ACK, self.server_ip, assigned,
                    self.subnet, self.gateway, self.lease_seconds,
                )
                srv.sendto(reply, ('255.255.255.255', 68))
                # Key: signal the GUI immediately so SSH probe starts at once
                self.device_leased.emit(assigned, mac)
            else:
                self.log_message.emit(
                    f"DHCP REQUEST from {mac} → NAK "
                    f"(requested={requested}, assigned={assigned})")
                nak = _build_dhcp_nak(pkt, self.server_ip)
                srv.sendto(nak, ('255.255.255.255', 68))


# ─────────────────────────────────────────────────────────────────────────────
#  Single-IP Probe Worker  (triggered by a DHCP lease event)
# ─────────────────────────────────────────────────────────────────────────────

class SingleProbeWorker(QThread):
    """
    Waits for SSH to become available on *ip* (the router may still be
    finishing its boot), then gathers identity / version / MAC and reports
    the result via router_found so the GUI can add a table row immediately.
    """

    router_found = pyqtSignal(object)  # RouterInfo instance
    log_message  = pyqtSignal(str)

    _WAIT_INTERVAL = 3    # seconds between TCP-22 retries while booting
    _WAIT_MAX      = 180  # seconds before giving up

    def __init__(self, ip: str, mac: str,
                 ssh_user: str, ssh_pass: str, parent=None):
        super().__init__(parent)
        self.ip       = ip
        self.mac      = mac
        self.ssh_user = ssh_user
        self.ssh_pass = ssh_pass

    def run(self):
        ip = self.ip
        self.log_message.emit(
            f"[DHCP→SSH] {ip} ({self.mac}) — waiting for SSH port…")

        deadline = time.monotonic() + self._WAIT_MAX
        while time.monotonic() < deadline:
            try:
                with socket.create_connection((ip, SSH_PORT), timeout=2):
                    break
            except OSError:
                time.sleep(self._WAIT_INTERVAL)
        else:
            self.log_message.emit(
                f"[DHCP→SSH] {ip} — SSH did not open within "
                f"{self._WAIT_MAX}s; giving up.")
            return

        router = RouterInfo(ip=ip, mac=self.mac)
        try:
            details = _gather_router_info(ip, self.ssh_user, self.ssh_pass)
            router.identity = details["identity"]
            router.version  = details["version"]
            if details["mac"] != "—":
                router.mac = details["mac"]
        except Exception as exc:
            log.warning("[%s] Post-DHCP SSH probe failed: %s", ip, exc)
            router.identity = "(auth failed?)"

        self.log_message.emit(
            f"[DHCP→SSH] {ip} online — "
            f"identity={router.identity}  version={router.version}")
        self.router_found.emit(router)


# ─────────────────────────────────────────────────────────────────────────────
#  Main Window
# ─────────────────────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("MikroTik Bulk Provisioner")
        self.resize(1150, 780)

        # Application state
        self._routers:             Dict[str, RouterInfo]       = {}
        self._scan_worker:         Optional[ScanWorker]        = None
        self._provision_workers:   List[ProvisionWorker]       = []
        self._provision_semaphore: Optional[threading.Semaphore] = None
        self._active_provisions:   int                         = 0
        self._url_check_worker:    Optional[UrlCheckWorker]    = None
        self._dhcp_worker:         Optional[DhcpServerWorker]   = None
        self._probe_workers:       List[SingleProbeWorker]      = []

        self._build_ui()
        self._connect_signals()
        self._log(f"MikroTik Bulk Provisioner started. Log file: {os.path.abspath(LOG_FILE)}")

    # ── UI construction ──────────────────────────────────────────────────────

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setSpacing(8)
        root.setContentsMargins(10, 10, 10, 10)

        # ── Settings row ─────────────────────────────────────────────────────
        settings_row = QHBoxLayout()

        ip_grp = QGroupBox("IP Range")
        ip_lay = QHBoxLayout(ip_grp)
        self.le_ip_start = QLineEdit(DEFAULT_IP_START)
        self.le_ip_start.setFixedWidth(135)
        self.le_ip_end   = QLineEdit(DEFAULT_IP_END)
        self.le_ip_end.setFixedWidth(135)
        ip_lay.addWidget(QLabel("Start:"))
        ip_lay.addWidget(self.le_ip_start)
        ip_lay.addWidget(QLabel("End:"))
        ip_lay.addWidget(self.le_ip_end)

        cred_grp = QGroupBox("SSH Credentials")
        cred_lay = QHBoxLayout(cred_grp)
        self.le_ssh_user = QLineEdit(DEFAULT_SSH_USER)
        self.le_ssh_user.setFixedWidth(80)
        self.le_ssh_pass = QLineEdit(DEFAULT_SSH_PASS)
        self.le_ssh_pass.setEchoMode(QLineEdit.EchoMode.Password)
        self.le_ssh_pass.setFixedWidth(130)
        self.le_ssh_pass.setPlaceholderText("(blank = no password)")
        cred_lay.addWidget(QLabel("User:"))
        cred_lay.addWidget(self.le_ssh_user)
        cred_lay.addWidget(QLabel("Password:"))
        cred_lay.addWidget(self.le_ssh_pass)

        conc_grp = QGroupBox("Concurrency")
        conc_lay = QHBoxLayout(conc_grp)
        self.sb_concurrency = QSpinBox()
        self.sb_concurrency.setRange(1, 30)
        self.sb_concurrency.setValue(DEFAULT_CONCURRENCY)
        self.sb_concurrency.setFixedWidth(65)
        conc_lay.addWidget(QLabel("Max parallel:"))
        conc_lay.addWidget(self.sb_concurrency)

        settings_row.addWidget(ip_grp)
        settings_row.addWidget(cred_grp)
        settings_row.addWidget(conc_grp)
        settings_row.addStretch()

        # ── Config URL ───────────────────────────────────────────────────────
        url_grp = QGroupBox("Config URL  (fetched by the router via /tool fetch)")
        url_lay = QHBoxLayout(url_grp)
        self.le_config_url = QLineEdit(DEFAULT_CONFIG_URL)
        self.btn_test_url  = QPushButton("Test HFS URL")
        self.btn_test_url.setMinimumHeight(28)
        self.btn_test_url.setToolTip(
            "Verify that the config file URL is reachable from this laptop")
        url_lay.addWidget(self.le_config_url)
        url_lay.addWidget(self.btn_test_url)

        # ── Built-in DHCP Server ─────────────────────────────────────────────
        dhcp_grp = QGroupBox(
            "Built-in DHCP Server  "
            "(disable tftpd64 DHCP when using this · requires Administrator)"
        )
        dhcp_outer = QVBoxLayout(dhcp_grp)
        dhcp_row1  = QHBoxLayout()
        dhcp_row2  = QHBoxLayout()

        self.le_dhcp_server_ip  = QLineEdit(DEFAULT_DHCP_SERVER_IP)
        self.le_dhcp_server_ip.setFixedWidth(115)
        self.le_dhcp_pool_start = QLineEdit(DEFAULT_DHCP_POOL_START)
        self.le_dhcp_pool_start.setFixedWidth(115)
        self.le_dhcp_pool_end   = QLineEdit(DEFAULT_DHCP_POOL_END)
        self.le_dhcp_pool_end.setFixedWidth(115)
        self.le_dhcp_subnet  = QLineEdit(DEFAULT_DHCP_SUBNET)
        self.le_dhcp_subnet.setFixedWidth(115)
        self.le_dhcp_gateway = QLineEdit(DEFAULT_DHCP_GATEWAY)
        self.le_dhcp_gateway.setFixedWidth(115)
        self.sb_dhcp_lease   = QSpinBox()
        self.sb_dhcp_lease.setRange(60, 86400)
        self.sb_dhcp_lease.setValue(DEFAULT_DHCP_LEASE)
        self.sb_dhcp_lease.setFixedWidth(75)

        self.btn_start_dhcp  = QPushButton("Start DHCP Server")
        self.btn_start_dhcp.setMinimumHeight(28)
        self.btn_stop_dhcp   = QPushButton("Stop DHCP Server")
        self.btn_stop_dhcp.setMinimumHeight(28)
        self.btn_stop_dhcp.setEnabled(False)
        self.lbl_dhcp_status = QLabel("Stopped")

        dhcp_row1.addWidget(QLabel("Server IP:"))
        dhcp_row1.addWidget(self.le_dhcp_server_ip)
        dhcp_row1.addWidget(QLabel("  Pool:"))
        dhcp_row1.addWidget(self.le_dhcp_pool_start)
        dhcp_row1.addWidget(QLabel("to"))
        dhcp_row1.addWidget(self.le_dhcp_pool_end)
        dhcp_row1.addStretch()

        dhcp_row2.addWidget(QLabel("Subnet Mask:"))
        dhcp_row2.addWidget(self.le_dhcp_subnet)
        dhcp_row2.addWidget(QLabel("  Gateway:"))
        dhcp_row2.addWidget(self.le_dhcp_gateway)
        dhcp_row2.addWidget(QLabel("  Lease:"))
        dhcp_row2.addWidget(self.sb_dhcp_lease)
        dhcp_row2.addWidget(QLabel("s"))
        dhcp_row2.addSpacing(10)
        dhcp_row2.addWidget(self.btn_start_dhcp)
        dhcp_row2.addWidget(self.btn_stop_dhcp)
        dhcp_row2.addSpacing(10)
        dhcp_row2.addWidget(QLabel("Status:"))
        dhcp_row2.addWidget(self.lbl_dhcp_status)
        dhcp_row2.addStretch()

        dhcp_outer.addLayout(dhcp_row1)
        dhcp_outer.addLayout(dhcp_row2)

        # ── Scan control bar ─────────────────────────────────────────────────
        scan_bar = QHBoxLayout()
        self.btn_scan      = QPushButton("Scan for Routers")
        self.btn_scan.setMinimumHeight(32)
        self.btn_stop_scan = QPushButton("Stop Scan")
        self.btn_stop_scan.setMinimumHeight(32)
        self.btn_stop_scan.setEnabled(False)
        self.btn_clear     = QPushButton("Clear Table")
        self.btn_clear.setMinimumHeight(32)
        self.lbl_scan      = QLabel("Ready.")
        scan_bar.addWidget(self.btn_scan)
        scan_bar.addWidget(self.btn_stop_scan)
        scan_bar.addWidget(self.btn_clear)
        scan_bar.addWidget(self.lbl_scan)
        scan_bar.addStretch()

        # ── Router table ─────────────────────────────────────────────────────
        self.table = QTableWidget()
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(
            ["✓", "IP Address", "MAC Address", "Identity",
             "RouterOS Version", "Status"]
        )
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(COL_CHECK,    QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(COL_IP,       QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(COL_MAC,      QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(COL_IDENTITY, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(COL_VERSION,  QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(COL_STATUS,   QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)

        # ── Action bar ───────────────────────────────────────────────────────
        action_bar = QHBoxLayout()
        self.btn_select_all   = QPushButton("Select All")
        self.btn_deselect_all = QPushButton("Deselect All")
        self.lbl_provision    = QLabel("")
        self.btn_provision    = QPushButton("Provision Selected")
        self.btn_provision.setMinimumHeight(34)
        self.btn_provision.setStyleSheet(
            "QPushButton { background-color: #0078d4; color: white; font-weight: bold; }"
            "QPushButton:hover { background-color: #005fa3; }"
            "QPushButton:disabled { background-color: #cccccc; color: #888888; }"
        )
        action_bar.addWidget(self.btn_select_all)
        action_bar.addWidget(self.btn_deselect_all)
        action_bar.addStretch()
        action_bar.addWidget(self.lbl_provision)
        action_bar.addWidget(self.btn_provision)

        # ── Log panel ────────────────────────────────────────────────────────
        log_grp = QGroupBox(f"Activity Log  (also saved to: {os.path.abspath(LOG_FILE)})")
        log_lay = QVBoxLayout(log_grp)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(3000)
        self.log_view.setFont(QFont("Consolas", 9))
        log_lay.addWidget(self.log_view)

        # ── Splitter: table + log ────────────────────────────────────────────
        splitter = QSplitter(Qt.Orientation.Vertical)

        table_panel = QWidget()
        tp_lay = QVBoxLayout(table_panel)
        tp_lay.setContentsMargins(0, 0, 0, 0)
        tp_lay.addLayout(scan_bar)
        tp_lay.addWidget(self.table)
        tp_lay.addLayout(action_bar)

        splitter.addWidget(table_panel)
        splitter.addWidget(log_grp)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)

        # ── Assemble root ────────────────────────────────────────────────────
        root.addLayout(settings_row)
        root.addWidget(url_grp)
        root.addWidget(dhcp_grp)
        root.addWidget(splitter)

    # ── Signal connections ───────────────────────────────────────────────────

    def _connect_signals(self):
        self.btn_scan.clicked.connect(self._start_scan)
        self.btn_stop_scan.clicked.connect(self._stop_scan)
        self.btn_clear.clicked.connect(self._clear_table)
        self.btn_select_all.clicked.connect(self._select_all)
        self.btn_deselect_all.clicked.connect(self._deselect_all)
        self.btn_provision.clicked.connect(self._start_provisioning)
        self.btn_test_url.clicked.connect(self._test_hfs_url)
        self.btn_start_dhcp.clicked.connect(self._start_dhcp_server)
        self.btn_stop_dhcp.clicked.connect(self._stop_dhcp_server)

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _log(self, message: str):
        """Append a timestamped line to the in-app log panel."""
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        self.log_view.appendPlainText(f"[{ts}] {message}")
        log.info(message)

    def _add_router_row(self, router: RouterInfo):
        """Insert a new row in the table for *router* (main thread only)."""
        row = self.table.rowCount()
        self.table.insertRow(row)
        router.row = row

        # Checkbox column
        chk = QTableWidgetItem()
        chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
        chk.setCheckState(Qt.CheckState.Checked)
        chk.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self.table.setItem(row, COL_CHECK, chk)

        # Data columns
        for col, text in (
            (COL_IP,       router.ip),
            (COL_MAC,      router.mac),
            (COL_IDENTITY, router.identity),
            (COL_VERSION,  router.version),
            (COL_STATUS,   router.status),
        ):
            item = QTableWidgetItem(text)
            item.setTextAlignment(
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft
            )
            self.table.setItem(row, col, item)

        self._color_row(row, router.status)

    def _update_status(self, ip: str, status: str, error: str = ""):
        """Refresh the Status cell and row colour for *ip* (main thread only)."""
        router = self._routers.get(ip)
        if router is None or router.row < 0:
            return

        router.status = status
        router.error  = error

        display = status if not error else f"{status}: {error[:80]}"
        item = QTableWidgetItem(display)
        item.setTextAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        if error:
            item.setToolTip(error)   # full error visible on hover
        self.table.setItem(router.row, COL_STATUS, item)
        self._color_row(router.row, status)

    def _color_row(self, row: int, status: str):
        """Paint all cells in *row* with the colour that matches *status*."""
        key = status if status in STATUS_COLORS else (
            "Failed" if "Failed" in status else "Discovered"
        )
        bg = QColor(STATUS_COLORS[key])
        fg = QColor(STATUS_FG.get(key, "#000000"))
        for col in range(self.table.columnCount()):
            item = self.table.item(row, col)
            if item:
                item.setBackground(bg)
                item.setForeground(fg)

    def _selected_routers(self) -> List[RouterInfo]:
        """Return RouterInfo objects whose checkbox is ticked."""
        result = []
        for row in range(self.table.rowCount()):
            chk = self.table.item(row, COL_CHECK)
            if chk and chk.checkState() == Qt.CheckState.Checked:
                ip_item = self.table.item(row, COL_IP)
                if ip_item:
                    router = self._routers.get(ip_item.text())
                    if router:
                        result.append(router)
        return result

    # ── HFS URL test ─────────────────────────────────────────────────────────

    def _test_hfs_url(self):
        url = self.le_config_url.text().strip()
        if not url:
            QMessageBox.warning(self, "No URL", "Config URL cannot be empty.")
            return
        self.btn_test_url.setEnabled(False)
        self.btn_test_url.setText("Testing…")
        self._log(f"Testing URL: {url}")
        self._url_check_worker = UrlCheckWorker(url)
        self._url_check_worker.result.connect(self._on_url_check_done)
        self._url_check_worker.start()

    def _on_url_check_done(self, success: bool, message: str):
        self.btn_test_url.setEnabled(True)
        self.btn_test_url.setText("Test HFS URL")
        self._log(f"URL check result: {message}")
        if success:
            QMessageBox.information(self, "URL Reachable", message)
        else:
            QMessageBox.warning(self, "URL Not Reachable", message)

    # ── ARP MAC refresh ───────────────────────────────────────────────────────

    def _refresh_macs_from_arp(self):
        """
        Look up the Windows ARP cache and fill in any MAC addresses that SSH
        could not retrieve (shown as '—').  Called after a scan completes.
        """
        arp = _get_arp_table()
        if not arp:
            return
        updated = 0
        for ip, router in self._routers.items():
            if router.mac == "—" and ip in arp:
                router.mac = arp[ip]
                if router.row >= 0:
                    item = QTableWidgetItem(router.mac)
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft
                    )
                    self.table.setItem(router.row, COL_MAC, item)
                    self._color_row(router.row, router.status)
                updated += 1
        if updated:
            self._log(f"ARP lookup: updated {updated} MAC address(es).")

    # ── Scan ─────────────────────────────────────────────────────────────────

    def _start_scan(self):
        ip_start = self.le_ip_start.text().strip()
        ip_end   = self.le_ip_end.text().strip()
        ssh_user = self.le_ssh_user.text().strip()
        ssh_pass = self.le_ssh_pass.text()

        # Basic validation
        for addr in (ip_start, ip_end):
            try:
                ipaddress.IPv4Address(addr)
            except ValueError:
                QMessageBox.warning(self, "Invalid IP",
                                    f"'{addr}' is not a valid IPv4 address.")
                return

        self.btn_scan.setEnabled(False)
        self.btn_stop_scan.setEnabled(True)
        self.lbl_scan.setText("Scanning…")

        self._scan_worker = ScanWorker(ip_start, ip_end, ssh_user, ssh_pass)
        self._scan_worker.router_found.connect(self._on_router_found)
        self._scan_worker.progress.connect(self._on_scan_progress)
        self._scan_worker.scan_finished.connect(self._on_scan_finished)
        self._scan_worker.log_message.connect(self._log)
        self._scan_worker.start()

    def _stop_scan(self):
        if self._scan_worker:
            self._scan_worker.stop()
        self.lbl_scan.setText("Stopping scan…")
        self.btn_stop_scan.setEnabled(False)

    def _on_router_found(self, router: RouterInfo):
        # Ignore duplicates (e.g. if the user re-scans without clearing)
        if router.ip not in self._routers:
            self._routers[router.ip] = router
            self._add_router_row(router)

    def _on_scan_progress(self, completed: int, total: int):
        pct = int(completed / total * 100) if total else 0
        self.lbl_scan.setText(
            f"Scanning… {completed}/{total} ({pct}%)  —  {len(self._routers)} found"
        )

    def _on_scan_finished(self, count: int):
        self.btn_scan.setEnabled(True)
        self.btn_stop_scan.setEnabled(False)
        self.lbl_scan.setText(f"Scan complete — {count} router(s) found.")
        self._log(f"Scan finished. {count} router(s) discovered.")
        # Enrich any missing MACs from the Windows ARP cache
        self._refresh_macs_from_arp()

    # ── Built-in DHCP Server ─────────────────────────────────────────────────

    def _start_dhcp_server(self):
        server_ip  = self.le_dhcp_server_ip.text().strip()
        pool_start = self.le_dhcp_pool_start.text().strip()
        pool_end   = self.le_dhcp_pool_end.text().strip()
        subnet     = self.le_dhcp_subnet.text().strip()
        gateway    = self.le_dhcp_gateway.text().strip()
        lease      = self.sb_dhcp_lease.value()

        for label, addr in (
            ("Server IP",   server_ip),
            ("Pool start",  pool_start),
            ("Pool end",    pool_end),
            ("Subnet mask", subnet),
            ("Gateway",     gateway),
        ):
            try:
                ipaddress.IPv4Address(addr)
            except ValueError:
                QMessageBox.warning(
                    self, "Invalid Address",
                    f"'{addr}' is not a valid IPv4 address ({label}).")
                return

        if int(ipaddress.IPv4Address(pool_start)) > int(
                ipaddress.IPv4Address(pool_end)):
            QMessageBox.warning(self, "Invalid Pool",
                                "Pool start address must be ≤ pool end address.")
            return

        self.btn_start_dhcp.setEnabled(False)
        self.btn_stop_dhcp.setEnabled(True)
        self.lbl_dhcp_status.setText("Starting…")
        self._log(
            f"Starting DHCP server: {server_ip}  "
            f"pool {pool_start}–{pool_end}  mask {subnet}  gw {gateway}"
        )

        self._dhcp_worker = DhcpServerWorker(
            server_ip, pool_start, pool_end, subnet, gateway, lease)
        self._dhcp_worker.device_leased.connect(self._on_device_leased)
        self._dhcp_worker.log_message.connect(self._log)
        self._dhcp_worker.server_stopped.connect(self._on_dhcp_stopped)
        self._dhcp_worker.start()
        self.lbl_dhcp_status.setText("Running")

    def _stop_dhcp_server(self):
        if self._dhcp_worker and self._dhcp_worker.isRunning():
            self._dhcp_worker.stop()
        self.btn_stop_dhcp.setEnabled(False)
        self.lbl_dhcp_status.setText("Stopping…")

    def _on_dhcp_stopped(self):
        self.btn_start_dhcp.setEnabled(True)
        self.btn_stop_dhcp.setEnabled(False)
        self.lbl_dhcp_status.setText("Stopped")

    def _on_device_leased(self, ip: str, mac: str):
        """
        Called immediately when a DHCP ACK is sent to a device.
        Starts a SingleProbeWorker that waits for SSH and then populates
        the table row — no manual scan needed.
        """
        self._log(f"[DHCP] Lease issued: {ip}  MAC={mac} — probing SSH…")
        if ip in self._routers:
            # Already known from a previous scan; skip duplicate probe.
            self._log(f"[DHCP] {ip} already in table — skipping re-probe.")
            return
        ssh_user = self.le_ssh_user.text().strip()
        ssh_pass = self.le_ssh_pass.text()
        worker = SingleProbeWorker(ip, mac, ssh_user, ssh_pass)
        worker.router_found.connect(self._on_router_found)
        worker.log_message.connect(self._log)
        worker.finished.connect(self._cleanup_probe_workers)
        self._probe_workers.append(worker)
        worker.start()

    def _cleanup_probe_workers(self):
        """Remove finished SingleProbeWorker instances from the tracking list."""
        self._probe_workers = [
            w for w in self._probe_workers if w.isRunning()]

    def _clear_table(self):
        if self._scan_worker and self._scan_worker.isRunning():
            QMessageBox.information(self, "Scan Running",
                                    "Stop the scan before clearing the table.")
            return
        self.table.setRowCount(0)
        self._routers.clear()
        self.lbl_scan.setText("Table cleared.")
        self._log("Table cleared.")

    # ── Selection ────────────────────────────────────────────────────────────

    def _select_all(self):
        for row in range(self.table.rowCount()):
            chk = self.table.item(row, COL_CHECK)
            if chk:
                chk.setCheckState(Qt.CheckState.Checked)

    def _deselect_all(self):
        for row in range(self.table.rowCount()):
            chk = self.table.item(row, COL_CHECK)
            if chk:
                chk.setCheckState(Qt.CheckState.Unchecked)

    # ── Provisioning ─────────────────────────────────────────────────────────

    def _start_provisioning(self):
        selected   = self._selected_routers()
        config_url = self.le_config_url.text().strip()
        ssh_user   = self.le_ssh_user.text().strip()
        ssh_pass   = self.le_ssh_pass.text()
        concurrency = self.sb_concurrency.value()

        if not selected:
            QMessageBox.information(self, "Nothing Selected",
                                    "Tick at least one router to provision.")
            return
        if not config_url:
            QMessageBox.warning(self, "No URL", "Config URL cannot be empty.")
            return

        # Confirmation dialog
        answer = QMessageBox.question(
            self, "Confirm Provisioning",
            f"Provision {len(selected)} router(s) with:\n\n{config_url}\n\n"
            f"Concurrency: {concurrency}\n\nProceed?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        self.btn_provision.setEnabled(False)
        self.lbl_provision.setText(f"Provisioning {len(selected)} router(s)…")
        self._log(
            f"Starting provisioning: {len(selected)} router(s), "
            f"concurrency={concurrency}, url={config_url}"
        )

        self._provision_semaphore = threading.Semaphore(concurrency)
        self._provision_workers   = []
        self._active_provisions   = len(selected)

        for router in selected:
            self._update_status(router.ip, "Queued")
            worker = ProvisionWorker(
                router, ssh_user, ssh_pass, config_url,
                self._provision_semaphore,
            )
            worker.status_update.connect(self._on_provision_status)
            worker.log_message.connect(self._log)
            worker.finished.connect(self._on_provision_worker_done)
            self._provision_workers.append(worker)
            worker.start()

    def _on_provision_status(self, ip: str, status: str, error: str):
        self._update_status(ip, status, error)

    def _on_provision_worker_done(self):
        self._active_provisions -= 1
        if self._active_provisions <= 0:
            success = sum(1 for r in self._routers.values()
                          if r.status == "Success")
            failed  = sum(1 for r in self._routers.values()
                          if "Failed" in r.status)
            summary = f"Done — {success} succeeded, {failed} failed."
            self.lbl_provision.setText(summary)
            self._log(summary)
            self.btn_provision.setEnabled(True)

    # ── Window lifecycle ─────────────────────────────────────────────────────

    def closeEvent(self, event):
        """Stop any running threads gracefully before exiting."""
        if self._scan_worker and self._scan_worker.isRunning():
            self._scan_worker.stop()
            self._scan_worker.wait(3000)
        if self._dhcp_worker and self._dhcp_worker.isRunning():
            self._dhcp_worker.stop()
            self._dhcp_worker.wait(3000)
        for w in self._probe_workers:
            if w.isRunning():
                w.wait(3000)
        for w in self._provision_workers:
            if w.isRunning():
                w.wait(3000)
        event.accept()


# ─────────────────────────────────────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────────────────────────────────────

def _resource_path(relative: str) -> str:
    """
    Return the absolute path to a bundled resource.
    Works both during development (plain Python) and when frozen by PyInstaller
    (where temporary files are extracted to sys._MEIPASS at runtime).
    """
    base = getattr(sys, "_MEIPASS",
                   os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, relative)


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    # Set application icon (works in both dev and packaged EXE)
    icon_path = _resource_path(os.path.join("assets", "logo.png"))
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))

    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
