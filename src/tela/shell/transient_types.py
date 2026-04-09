"""Shared transient connection exception classifiers.

This module centralizes the canonical set of exception types and errno
values that represent transient connection failures eligible for bounded
retry during gateway convergence.

Architecture
============
- **Core** (tela/core/): No I/O, no external dependencies. Defines pure
  classification contracts consumed by Shell.
- **Shell** (tela/shell/): I/O boundaries (network, file system). Uses
  TRANSIENT_CONNECTION_EXCEPTIONS for runtime retry decisions.

TimeoutError Policy Divergence
==============================
The ``TimeoutError`` type is treated as TRANSIENT in the Shell retry path
(connect_cmd.py) because:

1. **Gateway convergence scenario**: When the gateway HTTP server is still
   starting up or temporarily unreachable, a ``TimeoutError`` indicates the
   server did not respond within the allotted time — a condition that may
   resolve as the server completes startup.

2. **Retry semantics**: Connection-timeout failures are inherently
   non-destructive and safe to retry with exponential backoff.

3. **Layer-specific rationale**: This differs from MCP tool classification
   (tela/core/classification.py), where ``TimeoutError`` may indicate a
   downstream service timeout that should propagate as a permanent failure
   rather than trigger retry.

The divergence is intentional and reflects the different failure modes and
retry contracts at each layer:

+------------------+-------------------+------------------------+
| Layer            | TimeoutError      | Rationale              |
+------------------+-------------------+------------------------+
| Shell (connect)  | TRANSIENT         | Gateway may be warming |
| Core (MCP tool)  | NON-TRANSIENT     | Service timeout        |
+------------------+-------------------+------------------------+
"""

from __future__ import annotations

import errno

# -----------------------------------------------------------------------------
# Transient exception types (builtin subclasses)
# -----------------------------------------------------------------------------
# These Python builtin subclasses represent OS-level connection failures that
# are safe to retry when the gateway is converging. They are used for type-based
# classification in _is_transient_url_error.
#
# NOTE: TimeoutError is included here (not in NON_TRANSIENT_TYPES) because
# connection-timeout during gateway startup is recoverable. See policy
# divergence note above.

TRANSIENT_CONNECTION_EXCEPTIONS: tuple[type[Exception], ...] = (
    ConnectionRefusedError,
    ConnectionResetError,
    ConnectionAbortedError,
    BrokenPipeError,
    TimeoutError,  # Connection-timeout during gateway convergence
)

# -----------------------------------------------------------------------------
# Transient errno values (numeric OS errnos)
# -----------------------------------------------------------------------------
# These numeric errno values are used as a fallback when the exception does not
# have a dedicated subclass but the OS still provides a numeric code.

TRANSIENT_ERRNOS: frozenset[int] = frozenset(
    {
        errno.ECONNREFUSED,  # Connection refused
        errno.ECONNRESET,  # Connection reset by peer
        errno.ECONNABORTED,  # Connection aborted
        errno.EPIPE,  # Broken pipe
        errno.ETIMEDOUT,  # Operation timed out
    }
)

# Alias for backward compatibility
TRANSIENT_CONNECTION_ERRNOS = TRANSIENT_ERRNOS
