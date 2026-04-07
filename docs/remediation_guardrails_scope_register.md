# Remediation Guardrails Scope Register

## Purpose

Define the exact remediation tranche boundary for `remediation_guardrails.lock_scope`.

## In Scope

1. **Downstream passthrough wrapper removal**
   - Remove only wrapper layers that merely pass through to downstream helpers without adding durable boundary value.

2. **Shared error constant/helper centralization**
   - Centralize repeated error strings and small classification helpers where they are already shared across route and gateway surfaces.

3. **Duplicate route/gateway error classification cleanup**
   - Eliminate duplicated HTTP status / error-prefix classification logic between route handlers and gateway route wiring.

4. **Shared CLI/env bearer precedence extraction**
   - Extract the common bearer-token precedence policy used by `tela serve` and `tela connect` without changing their surface-specific fallback behavior.

5. **Gateway route auth skeleton extraction**
   - Extract the repeated route-level auth/request-validation skeleton used by gateway HTTP routes.

## Keep-Separate Constraints

1. **Request-vs-ASGI auth extractors remain separate**
   - `src/tela/shell/gateway_http_auth.py::extract_bearer_token(Request)` stays request-surface specific.
   - `src/tela/shell/http_auth.py::BearerAuthMiddleware._extract_bearer_token(scope)` stays ASGI-middleware specific.
   - Rationale: they consume different boundary objects (`Request` vs raw ASGI `Scope`) and sit in different lifecycle layers (route helper vs middleware guard).

2. **No general auth architecture rewrite**
   - This tranche may extract shared constants/helpers/skeletons only.
   - It must not collapse route auth, middleware auth, and broader gateway auth concerns into a new generalized auth subsystem.

## Deferred Item

1. **Downstream recovery-envelope construction abstraction**
   - Repetitive recovery-envelope construction in `src/tela/shell/downstream.py` is explicitly deferred.
   - Rationale: the recovery logic is larger and riskier than the guardrail cleanup targeted in this tranche; abstracting it now would broaden scope from deduplication into recovery-flow redesign.

## Boundary Notes

- Shared bearer precedence extraction is limited to the precedence rule itself.
- `tela serve` may still generate a token when CLI/env are absent.
- `tela connect` may still fall back to lockfile token or emit its current missing-token error.
- Error helper centralization is allowed only where it removes already-duplicated route/gateway classifications.
- No work in this tranche may merge Request-based route auth with raw ASGI middleware auth.
