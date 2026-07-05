"""Constants for the Oppo UDP-20X integration."""

DOMAIN = "oppo_udp"

CONF_MODEL = "model"
CONF_MAC = "mac"

DEFAULT_PORT = 23
# Magnetar players listen for network control on a fixed port.
MAGNETAR_PORT = 8102
DEFAULT_TIMEOUT = 3.0

# Models
MODEL_UDP203 = "UDP-203"
MODEL_UDP205 = "UDP-205"
MODEL_MAGNETAR = "Magnetar"

MODELS = [MODEL_UDP203, MODEL_UDP205, MODEL_MAGNETAR]

# Models that speak the stateless Magnetar network-control protocol
# (fire-and-forget commands on MAGNETAR_PORT, no query/streaming support).
MAGNETAR_MODELS = frozenset({MODEL_MAGNETAR})

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
