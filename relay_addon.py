"""Entry point shim for mitmdump --scripts relay_addon.py."""

from copilot_app_proxy.relay_addon import TARGET_HOST, TARGET_PATH_PREFIX, TokenRelayAddon, addons

__all__ = ["TARGET_HOST", "TARGET_PATH_PREFIX", "TokenRelayAddon", "addons"]

