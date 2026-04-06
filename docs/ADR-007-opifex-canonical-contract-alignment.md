# ADR-007: Opifex Canonical Contract Alignment

## Status

Accepted

## Implementation Status

Documentation in this repository is aligned to the accepted canonical direction.
Implementation may still lag this decision during remediation. Where current
runtime models or wire fields still use legacy naming, treat ADR-007 as the
target contract direction and `../opifex` as the authority for shared meaning.

## Context

`mcp-tela` participates in a cross-repo remediation program where `../opifex`
is the canonical contract authority for shared surfaces.

Review of the current repository found drift across several shared boundaries:

- token binding vocabulary diverged across `profile_id`, `profile_name`, and
  `tools_profile`
- `_meta` semantics were not documented with a sharp enough split between audit
  carrier fields and auth/authz behavior
- repo-local and shared-facing surfaces were not consistently distinguished in
  documentation
- local schema copies and examples risked being read as alternate contract
  truth

The agreed project principles are:

1. `../opifex` is the constitution and must be followed strictly
2. backward compatibility is not an architecture goal; the clean canonical
   shape wins
3. `mcp-tela` must remain independently usable after release, but must not
   become a second authority for shared contract meaning

## Decision Drivers

- preserve a single source of truth for shared contracts
- keep issuer/carrier/verifier symmetry across `nervus`, `anima`, and `tela`
- prevent repo-local convenience names from leaking into shared boundaries
- keep `tela` an authz verifier/enforcer and audit sink, not a second trace or
  persona authority
- allow independent `tela` distribution without introducing a second editable
  schema truth source

## Options Considered

### Option A: Strict canonical alignment with repo-local/display-only aliases
- **Mechanism**: shared token and audit-facing identity uses canonical
  `profile_id`; `_meta` semantics come from `opifex/contracts/meta.schema.json`;
  `persona_ref` remains a signed token field and an audit/reference field in
  `_meta`; any local `profile_name` use is display-only and never canonical.
- **Pros**: single truth source; clean verifier boundary; no alias debt;
  minimizes cross-repo semantic ambiguity.
- **Cons**: requires explicit remediation of drifted local docs and local
  implementation surfaces.
- **Fails if**: `tela` still has to guess what a token or `_meta` field means at
  verification time.

### Option B: Layered canonical-plus-legacy vocabulary
- **Mechanism**: treat `profile_id` as canonical but continue documenting
  `profile_name`/`tools_profile` as acceptable shared-surface aliases during a
  compatibility window.
- **Pros**: lower short-term migration friction.
- **Cons**: preserves dual truth; weakens issuer/verifier symmetry; creates open-
  ended contract debt.
- **Fails if**: sibling repos continue to consume different profile fields as if
  they were semantically equivalent.

## Decision

Choose **Option A**.

I chose it because the remediation program explicitly requires `mcp-tela` to be
a conformance target, not a contract co-author. The clean architecture is:

- `opifex/contracts/capability_token.schema.json` is the only canonical shared
  token contract
- `CapabilityToken.profile_id` is the only canonical bound profile identity on
  shared token/verifier surfaces
- `tools_profile` is invalid on shared token surfaces
- `profile_name`, if retained at all, is repo-local display vocabulary only and
  must not act as the bound identity on shared surfaces
- `CapabilityToken.persona_ref` is a canonical signed token field
- `_meta.persona_ref` is supplemental audit/reference context only
- `_meta` does not participate in authentication or authorization decisions
- `tela` records canonical audit fields, including trace correlation, but does
  not become trace authority
- shared-facing errors must align to `opifex/contracts/errors.yaml`
- independent `tela` distribution may include a read-only packaged snapshot of
  canonical schemas, but `mcp-tela` must not maintain a second editable schema
  authority

## Consequences

- documentation in `mcp-tela` must distinguish shared canonical surfaces from
  repo-local implementation surfaces
- drifted docs that treat `profile_name` or `tools_profile` as shared token
  vocabulary must be corrected
- audit-facing documentation must reflect canonical `profile_id` identity and
  canonical `_meta` semantics
- any local examples that show `profile_name` may only do so as display-only
  local vocabulary, not as token binding truth
- build/release processes may vendor canonical schema snapshots for standalone
  distribution, but maintenance authority stays in `../opifex`
