"""Constants for the Oppo UDP-20X integration."""

DOMAIN = "oppo_udp"

CONF_MODEL = "model"
CONF_MAC = "mac"

# UDP-20X players listen for network control on port 23.
DEFAULT_PORT = 23
# Magnetar players listen for network control on a fixed port.
MAGNETAR_PORT = 8102
# Pre-20X players listen on model-specific ports (see MODEL_DEFAULT_PORTS).
PORT_BDP83 = 19999
PORT_BDP9X_10X = 48360
DEFAULT_TIMEOUT = 3.0

# Models
MODEL_BDP83 = "BDP-83"
MODEL_BDP9X = "BDP-93/95"
MODEL_BDP10X = "BDP-103/105"
MODEL_UDP203 = "UDP-203"
MODEL_UDP205 = "UDP-205"
MODEL_MAGNETAR = "Magnetar"

MODELS = [
    MODEL_BDP83,
    MODEL_BDP9X,
    MODEL_BDP10X,
    MODEL_UDP203,
    MODEL_UDP205,
    MODEL_MAGNETAR,
]

# Models that speak the stateless Magnetar network-control protocol
# (fire-and-forget commands on MAGNETAR_PORT, no query/streaming support).
MAGNETAR_MODELS = frozenset({MODEL_MAGNETAR})

# Pre-20X players (BDP-83/93/95/103/105) share the Oppo RS-232 command codes but
# frame them over IP as ``REMOTE <CODE>`` (no ``#`` prefix, no CR) on their own
# ports. Responses, verbose mode and status updates are identical to the 20X.
PRE_20X_MODELS = frozenset({MODEL_BDP83, MODEL_BDP9X, MODEL_BDP10X})

# Default TCP control port per model (used when the user leaves the port field
# at the UDP-20X default). Magnetar handles its own port elsewhere.
MODEL_DEFAULT_PORTS = {
    MODEL_BDP83: PORT_BDP83,
    MODEL_BDP9X: PORT_BDP9X_10X,
    MODEL_BDP10X: PORT_BDP9X_10X,
    MODEL_UDP203: DEFAULT_PORT,
    MODEL_UDP205: DEFAULT_PORT,
}

# Input sources per model
INPUT_SOURCES_UDP203 = {
    "Blu-Ray Player": 0,
    "HDMI In": 1,
    "ARC HDMI Out": 2,
}

INPUT_SOURCES_UDP205 = {
    "Blu-Ray Player": 0,
    "HDMI In": 1,
    "Optical": 3,
    "Coaxial": 4,
    "USB Audio": 5,
}

# BDP-103/105 input sources (SIS digit values). BDP-83/93/95 have no
# input-source query/select support.
INPUT_SOURCES_BDP10X = {
    "Blu-Ray Player": 0,
    "HDMI Front": 1,
    "HDMI Back": 2,
    "ARC HDMI Out 1": 3,
    "ARC HDMI Out 2": 4,
    "Optical": 5,
    "Coaxial": 6,
    "USB Audio": 7,
}
