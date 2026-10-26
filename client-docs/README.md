# Client guides

Print-ready PDFs for the people whose computers you support. Send them with a
download link, or print them for a site visit.

| File | What it covers |
|---|---|
| `install-remote-support.pdf` | Installing the client from a download link on Windows, macOS and Linux, with pictures of each prompt (SmartScreen, User Account Control, Gatekeeper, macOS screen-recording and accessibility permissions) |
| `getting-help.pdf` | Finding the app and the device ID, accepting or dismissing a connection, ending a session, privacy, troubleshooting |

The pictures are drawn illustrations, not screenshots, so they stay sharp in
print and carry no real device or customer data.

## Rebuilding with your details

The committed PDFs leave the company, phone and email as blank lines to fill in
by hand. To print them instead, or to use a client group's branded app name:

```bash
pip install reportlab
python3 client-docs/build_client_docs.py \
  --company "Your Company" --phone "(555) 010-2030" --email help@example.com \
  --host rds.example.com --app-name "Remote Support"
```

`--out <folder>` writes the PDFs somewhere else, for example one folder per
client group.
