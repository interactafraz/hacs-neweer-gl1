"""Constants for the Neewer WiFi integration."""

DOMAIN = "neewer_wifi"

DEFAULT_PORT = 5052
HANDSHAKE_REPEAT = 3
DEFAULT_COMMAND_DELAY = 0.5
HEARTBEAT_INTERVAL = 2.0
HEARTBEAT_MISS_THRESHOLD = 3  # consecutive missed heartbeats before reconnecting
REHANDSHAKE_INTERVAL = 1800.0  # 30 minutes, fallback periodic re-handshake

MIN_BRIGHTNESS = 1
MAX_BRIGHTNESS = 100
MIN_COLOR_TEMP_PROTOCOL = 29
MAX_COLOR_TEMP_PROTOCOL = 70
MIN_COLOR_TEMP_KELVIN = 2900
MAX_COLOR_TEMP_KELVIN = 7000

DEFAULT_MODEL = "Neewer GL1 Pro"

CONF_HOST = "host"
CONF_CLIENT_IP = "client_ip"
CONF_SUBNET = "subnet"

PROBE_TIMEOUT = 2.0
DISCOVERY_CONCURRENCY = 30
MAX_SCAN_DURATION = 45.0
MIN_ROUTE_SCAN_PREFIXLEN = 16
PROC_NET_ROUTE = "/proc/net/route"
SKIP_ROUTE_IFACE_PREFIXES = ("docker", "br-", "veth", "virbr", "vmnet", "tun", "tap", "wg")

HEARTBEAT_ACK = bytes([0x80, 0x03, 0x00, 0x83])
WAKEUP_PACKET = bytes([0x80, 0x06, 0x01, 0x01, 0x88])
HEARTBEAT_PACKET = bytes([0x80, 0x04, 0x00, 0x84])
POWER_ON_PACKET = bytes([0x80, 0x05, 0x02, 0x01, 0x01, 0x89])
POWER_OFF_PACKET = bytes([0x80, 0x05, 0x02, 0x01, 0x00, 0x88])

PLATFORMS: list[str] = ["light"]
