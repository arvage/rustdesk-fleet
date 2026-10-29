# Landing page


static file with no build step: inline CSS and JS, fonts from Google Fonts.

The dashboard serves it at `/` to visitors who aren't signed in
(`subsystems/dashboard/app/routes/home.py`); signed-in staff get Server
Status there as before. Edits to this file show up on the next page load,
with no restart needed. It also works on any static host.

Preview locally:

```bash
python3 -m http.server -d site 8080   # then open http://localhost:8080
```

The device rows, customer names and device counts on the page are example
data, not fleet data.
