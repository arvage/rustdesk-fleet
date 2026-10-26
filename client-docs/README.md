# Client guides

Print-ready PDFs for the people whose computers you support. Send them with a
download link, or print them for a site visit.

| File | What it covers |
|---|---|
| `install-remote-support.pdf` | Installing the client from a download link on Windows, macOS and Linux, with pictures of each prompt (SmartScreen, User Account Control, Gatekeeper, macOS screen-recording and accessibility permissions) |
| `getting-help.pdf` | Finding the app and the device ID, accepting or dismissing a connection, ending a session, privacy, troubleshooting |

The pictures are drawn illustrations, not screenshots, so they stay sharp in
print and carry no real device or customer data.

The guides carry no IT support contact details, because the same guides go
to every tenant's clients.

## Generating guides for a tenant

`generate_guides()` writes both PDFs for one tenant, using the app name its
clients see and its server hostname. Tenant creation is meant to call it once
tenants exist (see [`VISION.md`](../VISION.md)); until then, run it by hand:

```python
from build_client_docs import generate_guides
generate_guides("/path/to/tenant/guides", app_name="Acme Remote Help", host="rds.acme.example")
```

Or from the command line:

```bash
pip install reportlab
python3 client-docs/build_client_docs.py --app-name "Acme Remote Help" \
  --host rds.acme.example --out /path/to/tenant/guides
```

With no options it rebuilds the default copies in this folder, which the
download page links today (`/guides/<file>`).
