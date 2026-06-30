# Signing

**Not started.**

Takes an unsigned installer (produced by a future `rdgen-cli` step inside
`subsystems/provisioning`) and signs it via Azure Trusted Signing /
Artifact Signing.

## Known constraint

Authenticode (PE/`.exe`) signing requires a Windows execution environment —
there's no Linux-native path through `signtool.exe` or the official
`dotnet sign` tool. Planned approach: trigger a GitHub Actions
`windows-latest` workflow from the Lightsail provisioning box, which signs
via `dotnet sign` against Trusted Signing and returns the signed binary.

A cross-platform alternative (`psign`, Rust-backed, portable mode) exists
and claims to sign PE files on Linux without Windows trust APIs, but is
unverified by us so far and the project itself documents feature gaps vs.
`signtool.exe`. Worth spiking directly against a real unsigned installer
before relying on it — see the architecture discussion for context.

## Prerequisites (one-time, not yet done)

- Azure Trusted Signing account + identity validation (driver's
  license/passport for individual developers; has a wait period — start
  this early, it's not on the code-dependency critical path but it IS on
  the timeline)
- A GitHub Actions service principal scoped to the
  `Trusted Signing Certificate Profile Signer` role on the specific signing
  account/resource group — not broader

## Not yet built

- Everything. This is a placeholder so the monorepo structure reflects the
  full architecture from day one.
