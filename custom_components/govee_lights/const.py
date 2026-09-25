"""
Global constants file
Used to store widely used information.
"""

DOMAIN = "govee_lights"
CONF_TYPE_BLE = 'BLE'
CONF_TYPE_LAN = 'LAN'

# Config entry data keys for LAN entries
CONF_LAN_IP = 'lan_ip'
CONF_LAN_DEVICE_ID = 'lan_device_id'
CONF_LAN_SKU = 'lan_sku'

# Config entry option keys: software crossfade durations (seconds). Govee
# firmware has no native fade; see fade.py. All default to 0 (instant).
CONF_FADE = 'fade'
CONF_FADE_ON = 'fade_on'
CONF_FADE_OFF = 'fade_off'
DEFAULT_FADE: float = 0.0
