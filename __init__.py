"""twitch-platform — Twitch chat adapter for the Hermes gateway.

EventSub WebSocket in (outbound-only — no public URL), Helix chat API out.
All credentials and accounts are per-install, configured through the
dashboard tab (device-activation flow, no OAuth redirect).
"""
from .adapter import register

__all__ = ["register"]
