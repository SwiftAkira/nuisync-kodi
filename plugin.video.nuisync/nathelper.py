"""
nathelper.py — NAT traversal helpers for NuiSync~

Provides serverless peer-to-peer connectivity:
    1. STUN client   — discover public IP:port via free Google/Cloudflare STUN
    2. UPnP/IGD      — auto-forward a port on the router (no manual config)
    3. Session codes  — encode IP:port into short human-readable codes
    4. UDP hole punch — punch through NATs when UPnP isn't available

No external dependencies — pure Python sockets + stdlib only.
"""

import os
import re
import socket
import struct
import threading
import time

try:
    from urllib.request import Request, urlopen
    from urllib.parse import urljoin
except ImportError:
    from urllib2 import Request, urlopen
    from urlparse import urljoin

try:
    import xml.etree.ElementTree as ET
except ImportError:
    ET = None

import xbmc

# ======================================================================
#  STUN — discover our public IP:port
# ======================================================================

STUN_SERVERS = [
    ("stun.l.google.com", 19302),
    ("stun1.l.google.com", 19302),
    ("stun.cloudflare.com", 3478),
    ("stun.services.mozilla.com", 3478),
]

STUN_MAGIC_COOKIE = 0x2112A442


def stun_request(local_port=0, server=None, timeout=3):
    """Send a STUN Binding Request and return (public_ip, public_port).

    Args:
        local_port: Bind to this local UDP port (0 = OS picks one).
        server:     (host, port) tuple.  None = try servers in order.
        timeout:    Seconds to wait for response.

    Returns:
        (ip_str, port_int) on success, None on failure.
    """
    servers = [server] if server else STUN_SERVERS

    for srv_host, srv_port in servers:
        try:
            return _stun_query(srv_host, srv_port, local_port, timeout)
        except Exception as exc:
            xbmc.log("[NuiSync] STUN %s:%d failed: %s" %
                     (srv_host, srv_port, exc), xbmc.LOGWARNING)
    return None


def _stun_query(host, port, local_port, timeout):
    """Single STUN Binding Request/Response exchange."""
    txn_id = os.urandom(12)
    header = struct.pack("!HHI", 0x0001, 0x0000, STUN_MAGIC_COOKIE) + txn_id

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(timeout)
        if local_port:
            sock.bind(("0.0.0.0", local_port))
        sock.sendto(header, (host, port))

        data, _ = sock.recvfrom(1024)
    finally:
        sock.close()

    # Validate response header
    if len(data) < 20:
        return None
    msg_type, msg_len, cookie = struct.unpack_from("!HHI", data, 0)
    resp_txn = data[8:20]
    if msg_type != 0x0101 or cookie != STUN_MAGIC_COOKIE or resp_txn != txn_id:
        return None

    # Walk TLV attributes looking for XOR-MAPPED-ADDRESS (0x0020)
    # or MAPPED-ADDRESS (0x0001) as fallback
    offset = 20
    mapped = None
    while offset + 4 <= 20 + msg_len:
        attr_type, attr_len = struct.unpack_from("!HH", data, offset)
        offset += 4
        if attr_type == 0x0020 and attr_len >= 8:
            # XOR-MAPPED-ADDRESS
            family = struct.unpack_from("!xBHI", data, offset)
            xport = family[1] ^ (STUN_MAGIC_COOKIE >> 16)
            xaddr = family[2] ^ STUN_MAGIC_COOKIE
            ip = socket.inet_ntoa(struct.pack("!I", xaddr))
            return (ip, xport)
        elif attr_type == 0x0001 and attr_len >= 8 and mapped is None:
            # MAPPED-ADDRESS (non-XOR fallback)
            family = struct.unpack_from("!xBHI", data, offset)
            mapped = (socket.inet_ntoa(struct.pack("!I", family[2])),
                      family[1])
        # Advance to next attribute (padded to 4-byte boundary)
        offset += attr_len
        if attr_len % 4:
            offset += 4 - (attr_len % 4)

    return mapped


def detect_nat_type(local_port=0):
    """Query two STUN servers to detect symmetric NAT.

    Returns:
        "symmetric" if the mapped port differs between servers,
        "cone" if both return the same mapping,
        None if detection fails.
    """
    results = []
    for srv in STUN_SERVERS[:2]:
        r = stun_request(local_port=local_port, server=srv, timeout=3)
        if r:
            results.append(r)
    if len(results) < 2:
        return None
    if results[0][1] != results[1][1]:
        return "symmetric"
    return "cone"


# ======================================================================
#  UPnP / IGD — auto-forward a port on the router
# ======================================================================

SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900

SSDP_MSEARCH = (
    "M-SEARCH * HTTP/1.1\r\n"
    "HOST: 239.255.255.250:1900\r\n"
    'MAN: "ssdp:discover"\r\n'
    "MX: 2\r\n"
    "ST: urn:schemas-upnp-org:device:InternetGatewayDevice:1\r\n"
    "\r\n"
)

# SOAP templates
_ADD_PORT_SOAP = """<?xml version="1.0"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"
 s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
<s:Body>
<u:AddPortMapping xmlns:u="{service_type}">
<NewRemoteHost></NewRemoteHost>
<NewExternalPort>{ext_port}</NewExternalPort>
<NewProtocol>{protocol}</NewProtocol>
<NewInternalPort>{int_port}</NewInternalPort>
<NewInternalClient>{int_ip}</NewInternalClient>
<NewEnabled>1</NewEnabled>
<NewPortMappingDescription>{description}</NewPortMappingDescription>
<NewLeaseDuration>{lease}</NewLeaseDuration>
</u:AddPortMapping>
</s:Body>
</s:Envelope>"""

_DEL_PORT_SOAP = """<?xml version="1.0"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"
 s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
<s:Body>
<u:DeletePortMapping xmlns:u="{service_type}">
<NewRemoteHost></NewRemoteHost>
<NewExternalPort>{ext_port}</NewExternalPort>
<NewProtocol>{protocol}</NewProtocol>
</u:DeletePortMapping>
</s:Body>
</s:Envelope>"""

_GET_EXT_IP_SOAP = """<?xml version="1.0"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"
 s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
<s:Body>
<u:GetExternalIPAddress xmlns:u="{service_type}">
</u:GetExternalIPAddress>
</s:Body>
</s:Envelope>"""


class UPnPMapping(object):
    """Manages a single UPnP port mapping on the gateway router."""

    def __init__(self):
        self._control_url = None
        self._service_type = None
        self._ext_port = None
        self._protocol = None
        self._active = False

    @property
    def active(self):
        return self._active

    def setup(self, ext_port, int_port, protocol="TCP",
              description="NuiSync", lease=0):
        """Discover the router and add a port mapping.

        Args:
            ext_port:    External port on the router.
            int_port:    Internal port on this machine.
            protocol:    "TCP" or "UDP".
            description: Human-readable mapping name.
            lease:       Duration in seconds (0 = indefinite).

        Returns:
            True if the mapping was created successfully.
        """
        try:
            location = self._ssdp_discover()
            if not location:
                xbmc.log("[NuiSync] UPnP: no gateway found", xbmc.LOGINFO)
                return False

            ctrl_url, svc_type = self._find_control_url(location)
            if not ctrl_url:
                xbmc.log("[NuiSync] UPnP: no WANIPConnection service",
                         xbmc.LOGWARNING)
                return False

            self._control_url = ctrl_url
            self._service_type = svc_type
            self._ext_port = ext_port
            self._protocol = protocol

            local_ip = self._get_local_ip()
            if not local_ip:
                xbmc.log("[NuiSync] UPnP: can't determine local IP",
                         xbmc.LOGWARNING)
                return False

            body = _ADD_PORT_SOAP.format(
                service_type=svc_type,
                ext_port=ext_port,
                protocol=protocol,
                int_port=int_port,
                int_ip=local_ip,
                description=description,
                lease=lease,
            )
            resp = self._soap_request(ctrl_url, svc_type,
                                      "AddPortMapping", body)
            if resp is not None:
                self._active = True
                xbmc.log("[NuiSync] UPnP: mapped %s %d -> %s:%d" %
                         (protocol, ext_port, local_ip, int_port),
                         xbmc.LOGINFO)
                return True
            return False

        except Exception as exc:
            xbmc.log("[NuiSync] UPnP setup error: %s" % exc,
                     xbmc.LOGWARNING)
            return False

    def get_external_ip(self):
        """Query the router for its external (WAN) IP address."""
        if not self._control_url or not self._service_type:
            return None
        try:
            body = _GET_EXT_IP_SOAP.format(service_type=self._service_type)
            resp = self._soap_request(self._control_url, self._service_type,
                                      "GetExternalIPAddress", body)
            if resp:
                match = re.search(
                    r"<NewExternalIPAddress>([^<]+)</NewExternalIPAddress>",
                    resp)
                if match:
                    return match.group(1)
        except Exception:
            pass
        return None

    def teardown(self):
        """Remove the port mapping from the router."""
        if not self._active or not self._control_url:
            return
        try:
            body = _DEL_PORT_SOAP.format(
                service_type=self._service_type,
                ext_port=self._ext_port,
                protocol=self._protocol,
            )
            self._soap_request(self._control_url, self._service_type,
                               "DeletePortMapping", body)
            xbmc.log("[NuiSync] UPnP: removed mapping %s %d" %
                     (self._protocol, self._ext_port), xbmc.LOGINFO)
        except Exception as exc:
            xbmc.log("[NuiSync] UPnP teardown error: %s" % exc,
                     xbmc.LOGWARNING)
        finally:
            self._active = False

    # -- Internal helpers --

    def _ssdp_discover(self, timeout=3):
        """Find the gateway's UPnP description URL via SSDP multicast."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM,
                             socket.IPPROTO_UDP)
        sock.settimeout(timeout)
        try:
            sock.sendto(SSDP_MSEARCH.encode(), (SSDP_ADDR, SSDP_PORT))
            while True:
                try:
                    data, _ = sock.recvfrom(4096)
                    text = data.decode("utf-8", errors="replace")
                    for line in text.split("\r\n"):
                        if line.lower().startswith("location:"):
                            return line.split(":", 1)[1].strip()
                except socket.timeout:
                    break
        finally:
            sock.close()
        return None

    def _find_control_url(self, location_url):
        """Parse the gateway XML to find the WANIPConnection control URL."""
        if ET is None:
            return None, None
        try:
            resp = urlopen(location_url, timeout=5)
            xml_data = resp.read()
        except Exception:
            return None, None

        try:
            root = ET.fromstring(xml_data)
        except ET.ParseError:
            return None, None

        # Search all <service> elements regardless of namespace
        ns_uri = "urn:schemas-upnp-org:device-1-0"
        tag_service = "{%s}service" % ns_uri
        tag_type = "{%s}serviceType" % ns_uri
        tag_ctrl = "{%s}controlURL" % ns_uri

        # Also try without namespace for non-compliant routers
        for service in list(root.iter(tag_service)) + list(root.iter("service")):
            stype_el = service.find(tag_type)
            if stype_el is None:
                stype_el = service.find("serviceType")
            if stype_el is None or stype_el.text is None:
                continue
            stype = stype_el.text
            if "WANIPConnection" in stype or "WANPPPConnection" in stype:
                ctrl_el = service.find(tag_ctrl)
                if ctrl_el is None:
                    ctrl_el = service.find("controlURL")
                if ctrl_el is not None and ctrl_el.text:
                    ctrl_url = urljoin(location_url, ctrl_el.text)
                    return ctrl_url, stype
        return None, None

    def _soap_request(self, control_url, service_type, action, body):
        """Send a SOAP request and return response body text, or None."""
        headers = {
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPAction": '"%s#%s"' % (service_type, action),
        }
        req = Request(control_url, data=body.encode("utf-8"), headers=headers)
        try:
            resp = urlopen(req, timeout=5)
            return resp.read().decode("utf-8", errors="replace")
        except Exception as exc:
            xbmc.log("[NuiSync] UPnP SOAP %s failed: %s" % (action, exc),
                     xbmc.LOGWARNING)
            return None

    @staticmethod
    def _get_local_ip():
        """Determine local LAN IP by connecting to a public address."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(2)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return None


# ======================================================================
#  Session codes — human-readable IP:port encoding
# ======================================================================

# Crockford Base32: removes I, L, O, U to avoid confusion
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_DECODE_MAP = {}
for _i, _c in enumerate(_CROCKFORD):
    _DECODE_MAP[_c] = _i
    _DECODE_MAP[_c.lower()] = _i
# Accept common confusions
_DECODE_MAP["i"] = _DECODE_MAP["I"] = 1   # I -> 1
_DECODE_MAP["l"] = _DECODE_MAP["L"] = 1   # L -> 1
_DECODE_MAP["o"] = _DECODE_MAP["O"] = 0   # O -> 0


def encode_session(ip, port):
    """Encode an IPv4 address and port into a 12-char session code.

    Format: XXXXX-XXXXX  (two groups of 5, Crockford Base32)

    Args:
        ip:   IPv4 address string (e.g. "203.0.113.42")
        port: Port number (0-65535)

    Returns:
        String like "C0A80-164AP"
    """
    parts = [int(x) for x in ip.split(".")]
    raw = struct.pack("!BBBBH", parts[0], parts[1], parts[2], parts[3], port)
    num = int.from_bytes(raw, "big")

    chars = []
    for _ in range(10):
        chars.append(_CROCKFORD[num & 0x1F])
        num >>= 5
    code = "".join(reversed(chars))
    return "%s-%s" % (code[:5], code[5:])


def decode_session(code):
    """Decode a session code back to (ip, port).

    Args:
        code: String like "C0A80-164AP" or "c0a80164ap" (flexible)

    Returns:
        (ip_str, port_int) or None on invalid input.
    """
    code = code.strip().replace("-", "").replace(" ", "")
    if len(code) != 10:
        return None

    num = 0
    for c in code:
        val = _DECODE_MAP.get(c)
        if val is None:
            return None
        num = (num << 5) | val

    raw = num.to_bytes(6, "big")
    a, b, c, d, port = struct.unpack("!BBBBH", raw)
    return ("%d.%d.%d.%d" % (a, b, c, d), port)


# ======================================================================
#  UDP Hole Punching
# ======================================================================

# Handshake magic bytes to identify NuiSync hole-punch packets
_PUNCH_MAGIC = b"NUIS"
_PUNCH_ACK = b"NUIA"
_PUNCH_INTERVAL = 0.2     # seconds between punch packets
_PUNCH_BURST = 15         # packets per attempt
_PUNCH_ROUNDS = 3         # total rounds to try
_PUNCH_ROUND_GAP = 1.0    # seconds between rounds


def udp_hole_punch(local_port, remote_ip, remote_port, timeout=15):
    """Attempt to punch a UDP hole to the remote peer.

    Both sides must call this simultaneously. The session code exchange
    serves as the coordination signal.

    Args:
        local_port:  Local UDP port (should match STUN-discovered port).
        remote_ip:   Remote peer's public IP.
        remote_port: Remote peer's public port.
        timeout:     Max seconds to wait for success.

    Returns:
        The connected UDP socket on success, None on failure.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("0.0.0.0", local_port))
    except OSError as exc:
        xbmc.log("[NuiSync] Hole punch bind failed: %s" % exc,
                 xbmc.LOGERROR)
        sock.close()
        return None

    sock.settimeout(0.3)
    target = (remote_ip, remote_port)
    deadline = time.time() + timeout

    xbmc.log("[NuiSync] Hole punching %s:%d from local :%d" %
             (remote_ip, remote_port, local_port), xbmc.LOGINFO)

    monitor = xbmc.Monitor()
    got_ack = False

    for _round in range(_PUNCH_ROUNDS):
        if time.time() > deadline or monitor.abortRequested():
            break

        # Send a burst of punch packets
        for _i in range(_PUNCH_BURST):
            try:
                sock.sendto(_PUNCH_MAGIC, target)
            except OSError:
                pass
            # Check for incoming between sends
            try:
                data, addr = sock.recvfrom(64)
                if data.startswith(_PUNCH_MAGIC):
                    # Peer's punch arrived! Send ACK back
                    xbmc.log("[NuiSync] Punch received from %s" % str(addr),
                             xbmc.LOGINFO)
                    for _ in range(5):
                        sock.sendto(_PUNCH_ACK, addr)
                    got_ack = True
                    break
                elif data.startswith(_PUNCH_ACK):
                    xbmc.log("[NuiSync] Punch ACK from %s" % str(addr),
                             xbmc.LOGINFO)
                    got_ack = True
                    break
            except socket.timeout:
                pass
            time.sleep(_PUNCH_INTERVAL)

        if got_ack:
            break

        # Wait between rounds, but keep listening
        round_end = time.time() + _PUNCH_ROUND_GAP
        while time.time() < round_end and not monitor.abortRequested():
            try:
                data, addr = sock.recvfrom(64)
                if data.startswith((_PUNCH_MAGIC, _PUNCH_ACK)):
                    for _ in range(5):
                        sock.sendto(_PUNCH_ACK, addr)
                    got_ack = True
                    break
            except socket.timeout:
                pass

        if got_ack:
            break

    if not got_ack:
        # Last chance: listen for a few more seconds
        listen_end = min(deadline, time.time() + 3)
        while time.time() < listen_end and not monitor.abortRequested():
            try:
                data, addr = sock.recvfrom(64)
                if data.startswith((_PUNCH_MAGIC, _PUNCH_ACK)):
                    for _ in range(5):
                        sock.sendto(_PUNCH_ACK, addr)
                    got_ack = True
                    break
            except socket.timeout:
                pass

    if got_ack:
        xbmc.log("[NuiSync] Hole punch succeeded!", xbmc.LOGINFO)
        sock.settimeout(None)
        return sock
    else:
        xbmc.log("[NuiSync] Hole punch failed after %d rounds" %
                 _PUNCH_ROUNDS, xbmc.LOGWARNING)
        sock.close()
        return None


# ======================================================================
#  Connection strategy orchestrator
# ======================================================================

def discover_public_address(local_port=0):
    """Discover our public IP:port via STUN.

    Returns:
        (public_ip, public_port) or None.
    """
    result = stun_request(local_port=local_port)
    if result:
        xbmc.log("[NuiSync] Public address: %s:%d" % result, xbmc.LOGINFO)
    return result


def try_upnp_forward(port, protocol="TCP"):
    """Try to set up a UPnP port forward. Returns UPnPMapping or None."""
    mapping = UPnPMapping()
    if mapping.setup(port, port, protocol=protocol):
        return mapping
    return None