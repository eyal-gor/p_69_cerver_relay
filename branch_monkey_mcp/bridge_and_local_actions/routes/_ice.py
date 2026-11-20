"""Faster WebRTC setup for direct mode and live screen.

aiortc waits up to 5 s for STUN replies on every network interface before
it answers. On a Mac some interfaces (VPN tunnels, IPv6 link-local) never
reply, so every answer took the full 5 s. A public STUN server replies in
well under a second, so cap the wait at 1.5 s: same routes found (measured
on a MacBook: 4 candidates either way), 3.5 s sooner.
"""

GATHER_SECONDS = 1.5


def speed_up_gathering() -> None:
    try:
        import aioice.ice as ice
    except ImportError:
        return
    fn = ice.Connection.get_component_candidates
    if fn.__defaults__ and fn.__defaults__[-1] != GATHER_SECONDS:
        fn.__defaults__ = fn.__defaults__[:-1] + (GATHER_SECONDS,)
