"""Constants for the Oppo UDP-20X integration."""

DOMAIN = "oppo_udp"

CONF_HOST = "host"
CONF_MODEL = "model"
CONF_PORT = "port"

DEFAULT_PORT = 23
DEFAULT_TIMEOUT = 3.0

# Models
MODEL_UDP203 = "UDP-203"
MODEL_UDP205 = "UDP-205"

MODELS = [MODEL_UDP203, MODEL_UDP205]

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
