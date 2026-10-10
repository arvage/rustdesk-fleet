# Signing

**Built (2026-10-10).** Windows installers are Authenticode-signed with
**Azure Trusted Signing** (renamed **Azure Artifact Signing**, Jan 2026) using
`jsign` **on the Linux dashboard box** — no Windows runner or GitHub Actions.

## How it works

- Engine: `subsystems/single-tenant/sign_installer.py`. Mints a short-lived
  Azure token from the service-principal client credentials
  (`login.microsoftonline.com/<tenant>/oauth2/v2.0/token`, scope
  `https://codesigning.azure.net/.default`), then runs:

  ```
  java -jar jsign-7.1.jar --storetype TRUSTEDSIGNING \
       --keystore <region>.codesigning.azure.net --storepass <token> \
       --alias <account>/<profile> \
       --tsaurl http://timestamp.acs.microsoft.com --replace <installer.exe>
  ```

- Config: `signing_config` singleton table, edited at **Admin → Signing**
  (tenant/client-id/secret, endpoint, account, certificate profile, enable +
  auto-sign). The client secret is stored like the backup secret.
- Trigger: auto-signs right after `build_installer()` when enabled + auto-sign,
  and a manual **Sign / Re-sign** button per installer on the group page
  (`generate_installer.sign_existing()`). On success the row goes to
  `status='signed'` with `signed_path` + `sha256_signed` + `signed_at`; on
  failure it reverts to `built` with `error_message`, so a bad sign never loses
  the usable unsigned build.
- Serving: download links already prefer `signed_path`, so signed builds are
  served automatically.

## Why this replaced the original plan

The earlier plan assumed Authenticode needed a Windows environment
(`signtool.exe` / `dotnet sign`) triggered via a GitHub Actions
`windows-latest` runner. `jsign` 7.0+ (Jan 2025) added native Trusted Signing
support and runs on the JVM, so the dashboard signs locally where it already
builds — simpler and no external CI dependency.

## Server prerequisites

- `default-jre-headless`
- `jsign` 7.x at `/opt/rustdesk-fleet/tools/jsign-7.1.jar`

## Azure prerequisites

See `DEPLOYMENT.md` → "Code signing (Azure Trusted Signing)" for the full
one-time Azure setup: the signing account, identity validation (has a wait
period), a certificate profile, a service principal, and the **Certificate
Profile Signer** role assignment.

## Not yet verified

- A real end-to-end sign against a live Trusted Signing account. The code,
  token request, and jsign invocation are verified; it needs the Azure account
  to confirm a produced signature validates on Windows.
