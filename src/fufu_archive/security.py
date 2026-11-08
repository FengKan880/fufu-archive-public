"""In-process capability labels for local queue operations.

IMPORTANT — this is NOT an authentication boundary.

These roles are plain strings supplied by the calling code. Nothing here
authenticates a principal, verifies an OS/service identity, checks a token, or
enforces anything against an untrusted caller: any caller able to import this
module can construct any role it likes. The checks below therefore only guard
against *accidental* misuse inside one trusted process (for example a worker
helper that should not pause the queue).

Real authorization (an authenticated principal plus end-to-end enforcement for
the CLI, HTTP and MCP entry points) is NOT implemented. Until it is, the CLI
performs no role check at all and instead fails closed behind an explicit
local-operator interlock, so that supplying the string "admin" can never grant
privileges.
"""
from __future__ import annotations

ROLES = frozenset({"reader", "worker", "admin"})
_CAPABILITIES = {
    "reader": frozenset({"inspect"}),
    "worker": frozenset({"execute"}),
    "admin": frozenset({"inspect", "execute", "management"}),
}


def require_capability(role: str, capability: str) -> None:
    """Guard an in-process call site against accidental role misuse.

    This is a self-imposed contract inside a trusted process, not a security
    control against an untrusted caller.
    """
    if role not in ROLES:
        raise PermissionError(f"unknown role: {role!r}")
    if capability not in _CAPABILITIES[role]:
        raise PermissionError(f"role {role!r} lacks {capability} permission")


__all__ = ["ROLES", "require_capability"]
