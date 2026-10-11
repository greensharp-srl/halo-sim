"""halo-sim: a phone-backed simulator for Brilliant Labs Halo glasses.

The Mac runs the virtual glasses (the official ``halo-emulator`` Lua VM plus
camera, live microphone, speaker and sensor bridges). A phone (or any browser)
connects over Wi-Fi and becomes the glasses' hardware: its camera, microphone,
motion sensors and speaker, the round display, the button and the temple taps.

Host apps written against ``brilliant-sdk`` run unmodified: ``halo-sim run
app.py`` swaps the SDK's Bluetooth class for one that talks to the simulator.
"""
__version__ = "0.1.0"

DEVICE_NAME = "Halo SIM"
DEFAULT_HTTP_PORT = 8080
DEFAULT_HTTPS_PORT = 8443
