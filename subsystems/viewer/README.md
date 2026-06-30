# Viewer

**Not started.**

Browser-based remote session viewer, built on RustDesk's own official web
client (TypeScript, WebCodecs hardware decode with WASM software fallback,
same protobuf wire protocol as native clients). Launched from the dashboard,
scoped to whichever tenant's `hbbr` the session belongs to.

Constraint to remember: browsers can't open raw TCP/UDP sockets, so every
browser-based session goes through the tenant's `hbbr` relay — there's no
P2P path for the web viewer the way there is for native clients. Relay
bandwidth sizing matters more here than it would for native-client usage.

## Not yet built

- Everything. This is a placeholder so the monorepo structure reflects the
  full architecture from day one.
