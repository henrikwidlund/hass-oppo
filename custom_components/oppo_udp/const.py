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

# Input source display names
BLU_RAY_PLAYER = "Blu-Ray Player"
HDMI_IN = "HDMI In"
ARC_HDMI_OUT = "ARC HDMI Out"
OPTICAL = "Optical"
COAXIAL = "Coaxial"
USB_AUDIO = "USB Audio"
# BDP-103/105 input source display names
HDMI_FRONT = "HDMI Front"
HDMI_BACK = "HDMI Back"
ARC_HDMI_OUT_1 = "ARC HDMI Out 1"
ARC_HDMI_OUT_2 = "ARC HDMI Out 2"

# Raw input-source response tokens returned by QIS/SIS (UDP-20X)
SRC_RESP_BD_PLAYER = "0 BD-PLAYER"
SRC_RESP_HDMI_IN = "1 HDMI-IN"
SRC_RESP_ARC_HDMI_OUT = "2 ARC-HDMI-OUT"
SRC_RESP_OPTICAL_IN = "3 OPTICAL-IN"
SRC_RESP_COAXIAL_IN = "4 COAXIAL-IN"
SRC_RESP_USB_AUDIO_IN = "5 USB-AUDIO-IN"
# Raw input-source response tokens returned by QIS/SIS (BDP-103/105)
SRC_RESP_HDMI_FRONT = "1 HDMI-FRONT"
SRC_RESP_HDMI_BACK = "2 HDMI-BACK"
SRC_RESP_ARC_HDMI_OUT_1 = "3 ARC-HDMI-OUT1"
SRC_RESP_ARC_HDMI_OUT_2 = "4 ARC-HDMI-OUT2"
SRC_RESP_OPTICAL = "5 OPTICAL"
SRC_RESP_COAXIAL = "6 COAXIAL"
SRC_RESP_USB_AUDIO = "7 USB-AUDIO"

# Input sources per model
INPUT_SOURCES_UDP203 = {
    BLU_RAY_PLAYER: 0,
    HDMI_IN: 1,
    ARC_HDMI_OUT: 2,
}

INPUT_SOURCES_UDP205 = {
    BLU_RAY_PLAYER: 0,
    HDMI_IN: 1,
    OPTICAL: 3,
    COAXIAL: 4,
    USB_AUDIO: 5,
}

# BDP-103/105 input sources (SIS digit values). BDP-83/93/95 have no
# input-source query/select support.
INPUT_SOURCES_BDP10X = {
    BLU_RAY_PLAYER: 0,
    HDMI_FRONT: 1,
    HDMI_BACK: 2,
    ARC_HDMI_OUT_1: 3,
    ARC_HDMI_OUT_2: 4,
    OPTICAL: 5,
    COAXIAL: 6,
    USB_AUDIO: 7,
}
