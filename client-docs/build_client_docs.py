#!/usr/bin/env python3
"""
build_client_docs.py — print-ready PDF guides for the people whose computers
you support (not for technicians).

    python3 build_client_docs.py
    python3 build_client_docs.py --company "Acme IT" --phone "(555) 010-2030" \
        --email help@acme-it.example --host rds.acme-it.example

Writes two PDFs next to this script:
  install-remote-support.pdf   how to install the client from a download link
  getting-help.pdf             finding your ID, accepting and ending a session

Needs reportlab (pip install reportlab). Pictures are drawn as vector
illustrations, so they stay sharp when printed. Anything left out of the
options prints as a blank line the client can fill in by hand.
"""
import argparse
from pathlib import Path

from reportlab.lib.colors import HexColor, white
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (BaseDocTemplate, Flowable, Frame, KeepTogether,
                                PageTemplate, Paragraph, Spacer, Table, TableStyle)

INK = HexColor("#1B2433")
MUTED = HexColor("#5B6474")
LINE = HexColor("#D5DAE3")
PAPER = HexColor("#F5F6F9")
ACCENT = HexColor("#4F46E5")
ACCENT_SOFT = HexColor("#EEF0FF")
CALLOUT = HexColor("#E8890C")
OK = HexColor("#16A34A")
DANGER = HexColor("#DC2626")
WIN_BLUE = HexColor("#0063B1")

PAGE_W, PAGE_H = letter
MARGIN = 0.75 * inch
CONTENT_W = PAGE_W - 2 * MARGIN
INDENT = 30          # body text and pictures sit right of the step number
FIG_W = CONTENT_W - INDENT

EXAMPLE_ID = "418 220 907"


# ── Text styles ──────────────────────────────────────────────────────────────

def _styles():
    s = {}
    s["title"] = ParagraphStyle("title", fontName="Helvetica-Bold", fontSize=26, leading=30, textColor=INK)
    s["lede"] = ParagraphStyle("lede", fontName="Helvetica", fontSize=12.5, leading=18, textColor=MUTED, spaceBefore=8)
    s["h2"] = ParagraphStyle("h2", fontName="Helvetica-Bold", fontSize=16, leading=20, textColor=INK,
                             spaceBefore=18, spaceAfter=6)
    s["body"] = ParagraphStyle("body", fontName="Helvetica", fontSize=11, leading=16, textColor=INK)
    s["step"] = ParagraphStyle("step", parent=s["body"], leftIndent=INDENT, spaceBefore=3)
    s["note"] = ParagraphStyle("note", parent=s["body"], fontSize=10, leading=14.5, textColor=MUTED)
    s["caption"] = ParagraphStyle("caption", fontName="Helvetica-Oblique", fontSize=8.5, leading=11,
                                  textColor=MUTED, leftIndent=INDENT, spaceBefore=4)
    s["cell"] = ParagraphStyle("cell", parent=s["body"], fontSize=10, leading=14)
    s["cellb"] = ParagraphStyle("cellb", parent=s["cell"], fontName="Helvetica-Bold")
    return s


S = _styles()


# ── Drawing helpers (canvas coordinates: origin bottom-left) ─────────────────

def rrect(c, x, y, w, h, r=4, fill=None, stroke=LINE, width=1):
    c.saveState()
    c.setLineWidth(width)
    if fill is not None:
        c.setFillColor(fill)
    if stroke is not None:
        c.setStrokeColor(stroke)
    c.roundRect(x, y, w, h, r, stroke=1 if stroke is not None else 0, fill=1 if fill is not None else 0)
    c.restoreState()


def text(c, x, y, s, size=9, color=INK, font="Helvetica", align="left"):
    c.saveState()
    c.setFont(font, size)
    c.setFillColor(color)
    if align == "center":
        c.drawCentredString(x, y, s)
    elif align == "right":
        c.drawRightString(x, y, s)
    else:
        c.drawString(x, y, s)
    c.restoreState()


def button(c, x, y, w, h, label, fill=ACCENT, color=white, stroke=None, size=9):
    rrect(c, x, y, w, h, 4, fill=fill, stroke=stroke)
    text(c, x + w / 2, y + h / 2 - size * 0.35, label, size, color, "Helvetica-Bold", "center")


def window(c, x, y, w, h, title, mac=False, bar=HexColor("#EEF0F4")):
    """Window frame with a title bar; returns the y of the content top."""
    rrect(c, x + 2, y - 3, w, h, 7, fill=HexColor("#E3E6EC"), stroke=None)   # soft shadow
    rrect(c, x, y, w, h, 7, fill=white, stroke=HexColor("#B9C0CC"))
    c.saveState()
    p = c.beginPath()
    p.roundRect(x, y, w, h, 7)
    c.clipPath(p, stroke=0)
    c.setFillColor(bar)
    c.rect(x, y + h - 20, w, 20, stroke=0, fill=1)
    c.restoreState()
    c.setStrokeColor(HexColor("#D0D5DE"))
    c.line(x, y + h - 20, x + w, y + h - 20)
    if mac:
        for i, col in enumerate(("#FF5F57", "#FEBC2E", "#28C840")):
            c.setFillColor(HexColor(col))
            c.circle(x + 12 + i * 13, y + h - 10, 3.6, stroke=0, fill=1)
        text(c, x + w / 2, y + h - 13.5, title, 8.5, MUTED, "Helvetica-Bold", "center")
    else:
        text(c, x + 10, y + h - 13.5, title, 8.5, INK)
        for i in range(3):   # minimise / maximise / close glyphs
            gx = x + w - 14 - i * 18
            c.setStrokeColor(MUTED)
            c.setLineWidth(0.8)
            if i == 0:
                c.line(gx - 3.5, y + h - 13.5, gx + 3.5, y + h - 6.5)
                c.line(gx - 3.5, y + h - 6.5, gx + 3.5, y + h - 13.5)
            elif i == 1:
                c.rect(gx - 3.5, y + h - 13.5, 7, 7, stroke=1, fill=0)
            else:
                c.line(gx - 3.5, y + h - 10, gx + 3.5, y + h - 10)
    return y + h - 20


def callout(c, x, y, w, h, n, corner="left"):
    """Orange ring around a control, with a numbered badge on its top corner."""
    bx = x - 4 if corner == "left" else x + w + 4
    c.saveState()
    c.setStrokeColor(CALLOUT)
    c.setLineWidth(2)
    c.roundRect(x - 4, y - 4, w + 8, h + 8, 6, stroke=1, fill=0)
    c.setFillColor(CALLOUT)
    c.circle(bx, y + h + 4, 8, stroke=0, fill=1)
    c.restoreState()
    text(c, bx, y + h + 0.8, str(n), 9, white, "Helvetica-Bold", "center")


def pointer(c, x, y):
    """Mouse pointer with its tip at (x, y)."""
    c.saveState()
    p = c.beginPath()
    pts = [(0, 0), (0, -15), (4, -11.5), (7, -18), (9.5, -17), (6.6, -10.6), (11.5, -10.6)]
    p.moveTo(x + pts[0][0], y + pts[0][1])
    for px, py in pts[1:]:
        p.lineTo(x + px, y + py)
    p.close()
    c.setFillColor(white)
    c.setStrokeColor(INK)
    c.setLineWidth(1)
    c.drawPath(p, stroke=1, fill=1)
    c.restoreState()


def arrow(c, x1, y, x2, color=MUTED):
    c.saveState()
    c.setStrokeColor(color)
    c.setFillColor(color)
    c.setLineWidth(1.6)
    c.line(x1, y, x2 - 6, y)
    p = c.beginPath()
    p.moveTo(x2, y)
    p.lineTo(x2 - 8, y + 5)
    p.lineTo(x2 - 8, y - 5)
    p.close()
    c.drawPath(p, stroke=0, fill=1)
    c.restoreState()


def app_icon(c, x, y, s=18):
    """The Remote Support app icon as drawn in these guides."""
    rrect(c, x, y, s, s, s * 0.24, fill=ACCENT, stroke=None)
    text(c, x + s / 2, y + s * 0.3, "R", s * 0.55, white, "Helvetica-Bold", "center")


def toggle(c, x, y, on=True):
    rrect(c, x, y, 26, 14, 7, fill=OK if on else LINE, stroke=None)
    c.setFillColor(white)
    c.circle(x + (19 if on else 7), y + 7, 5.3, stroke=0, fill=1)


# ── Pictures ─────────────────────────────────────────────────────────────────

class Figure(Flowable):
    def __init__(self, height, draw, **kw):
        super().__init__()
        self.width, self.height, self.draw_fn, self.kw = FIG_W, height, draw, kw

    def wrap(self, *_):
        return self.width, self.height

    def draw(self):
        c = self.canv
        c.translate(INDENT, 0)
        rrect(c, 0, 0, self.width, self.height, 8, fill=PAPER, stroke=None)
        self.draw_fn(c, self.width, self.height, **self.kw)


def fig_download_page(c, W, H, host):
    wx, wy, ww, wh = 40, 14, W - 80, H - 28
    top = window(c, wx, wy, ww, wh, "Remote Support - Download")
    # address bar
    rrect(c, wx + 10, top - 22, ww - 20, 16, 8, fill=PAPER, stroke=None)
    text(c, wx + 20, top - 17, f"https://{host}/d/k3V9...", 8, MUTED)
    # card
    cx, cw = wx + ww / 2 - 140, 280
    ch = 150
    cy = top - 34 - ch
    rrect(c, cx, cy, cw, ch, 8, fill=white, stroke=LINE)
    app_icon(c, cx + 16, cy + ch - 34, 20)
    text(c, cx + 44, cy + ch - 23, "Remote Support", 9.5, INK, "Helvetica-Bold")
    text(c, cx + 44, cy + ch - 34, "Your company", 7.5, MUTED)
    text(c, cx + 16, cy + ch - 56, "Download remote support", 11, INK, "Helvetica-Bold")
    text(c, cx + 16, cy + ch - 70, "Install this small program so your IT support team", 7.5, MUTED)
    text(c, cx + 16, cy + ch - 80, "can help you on this computer.", 7.5, MUTED)
    rows = [("Windows (64-bit)", True), ("macOS", False)]
    ry = cy + ch - 108
    for label, rec in rows:
        rrect(c, cx + 16, ry, cw - 32, 24, 4, fill=white, stroke=ACCENT if rec else LINE, width=1.4 if rec else 1)
        text(c, cx + 26, ry + 13, label, 8.5, INK, "Helvetica-Bold")
        if rec:
            text(c, cx + 104, ry + 13, "Recommended", 7, ACCENT, "Helvetica-Bold")
        text(c, cx + 26, ry + 4.5, "Version 1.4.2 · 24 MB", 6.5, MUTED)
        # download glyph
        c.setStrokeColor(ACCENT)
        c.setLineWidth(1.2)
        dx = cx + cw - 30
        c.line(dx, ry + 17, dx, ry + 8)
        c.line(dx - 3.5, ry + 11.5, dx, ry + 8)
        c.line(dx + 3.5, ry + 11.5, dx, ry + 8)
        c.line(dx - 5, ry + 6, dx + 5, ry + 6)
        ry -= 30
    callout(c, cx + 16, cy + ch - 108, cw - 32, 24, 1)
    pointer(c, cx + cw - 52, cy + ch - 100)


def fig_smartscreen(c, W, H):
    pw = (W - 70) / 2
    for i in range(2):
        x = 20 + i * (pw + 30)
        y, h = 14, H - 28
        rrect(c, x, y, pw, h, 3, fill=WIN_BLUE, stroke=None)
        text(c, x + 14, y + h - 30, "Windows protected your PC", 12, white, "Helvetica-Bold")
        text(c, x + 14, y + h - 48, "Microsoft Defender SmartScreen prevented an", 7.5, white)
        text(c, x + 14, y + h - 58, "unrecognized app from starting.", 7.5, white)
        if i == 0:
            text(c, x + 14, y + h - 78, "More info", 8, white, "Helvetica-Bold")
            c.setStrokeColor(white)
            c.line(x + 14, y + h - 80, x + 52, y + h - 80)
            callout(c, x + 14, y + h - 81, 40, 11, 1)
            pointer(c, x + 50, y + h - 80)
            button(c, x + pw - 84, y + 12, 70, 20, "Don't run", fill=HexColor("#E6E6E6"), color=INK)
        else:
            text(c, x + 14, y + h - 76, "App:  RemoteSupport-yourco-x64.exe", 7.5, white)
            text(c, x + 14, y + h - 88, "Publisher:  (your IT provider)", 7.5, white)
            button(c, x + pw - 166, y + 12, 74, 20, "Run anyway", fill=HexColor("#E6E6E6"), color=INK)
            button(c, x + pw - 84, y + 12, 70, 20, "Don't run", fill=HexColor("#E6E6E6"), color=INK)
            callout(c, x + pw - 166, y + 12, 74, 20, 2)
            pointer(c, x + pw - 100, y + 18)
    arrow(c, 20 + pw + 6, H / 2, 20 + pw + 26)


def fig_uac(c, W, H):
    w = 300
    x, y, h = (W - w) / 2, 14, H - 28
    rrect(c, x + 2, y - 3, w, h, 6, fill=HexColor("#E3E6EC"), stroke=None)
    rrect(c, x, y, w, h, 6, fill=white, stroke=HexColor("#B9C0CC"))
    c.saveState()
    p = c.beginPath()
    p.roundRect(x, y, w, h, 6)
    c.clipPath(p, stroke=0)
    c.setFillColor(HexColor("#F2F2F2"))
    c.rect(x, y, w, 40, stroke=0, fill=1)
    c.restoreState()
    text(c, x + 14, y + h - 20, "User Account Control", 7.5, MUTED)
    text(c, x + 14, y + h - 40, "Do you want to allow this app to make", 10.5, INK, "Helvetica-Bold")
    text(c, x + 14, y + h - 54, "changes to your device?", 10.5, INK, "Helvetica-Bold")
    app_icon(c, x + 14, y + h - 88, 22)
    text(c, x + 44, y + h - 75, "Remote Support", 9, INK, "Helvetica-Bold")
    text(c, x + 14, y + h - 104, "Verified publisher: (your IT provider)", 7.5, MUTED)
    button(c, x + w - 168, y + 10, 72, 20, "Yes", fill=HexColor("#0067C0"))
    button(c, x + w - 86, y + 10, 72, 20, "No", fill=HexColor("#E6E6E6"), color=INK)
    callout(c, x + w - 168, y + 10, 72, 20, 3)
    pointer(c, x + w - 106, y + 16)


def fig_mac_open(c, W, H):
    # Finder with right-click menu
    fw = W * 0.5
    top = window(c, 20, 14, fw, H - 28, "Downloads", mac=True, bar=HexColor("#ECECEC"))
    ry = top - 26
    rrect(c, 28, ry - 3, fw - 16, 16, 3, fill=HexColor("#2F6FEB"), stroke=None)
    text(c, 36, ry + 1.5, "RemoteSupport-yourco-macos.command", 7.5, white)
    text(c, 36, ry - 17, "Screenshot 2026-10-20.png", 7.5, MUTED)
    mx, my = 110, ry - 86
    rrect(c, mx, my, 104, 78, 5, fill=HexColor("#F7F7F7"), stroke=HexColor("#C9C9C9"))
    for j, item in enumerate(("Open", "Open With", "Move to Trash", "Get Info", "Rename")):
        iy = my + 64 - j * 14
        if j == 0:
            rrect(c, mx + 4, iy - 3.5, 96, 13, 3, fill=HexColor("#2F6FEB"), stroke=None)
        text(c, mx + 10, iy, item, 7.5, white if j == 0 else INK)
    callout(c, mx + 4, my + 60.5, 96, 13, 1)
    pointer(c, mx + 78, my + 62)
    # Gatekeeper prompt
    gx, gw = 20 + fw + 26, W - fw - 66
    gy, gh = 24, H - 48
    rrect(c, gx, gy, gw, gh, 10, fill=HexColor("#F2F2F2"), stroke=HexColor("#C9C9C9"))
    app_icon(c, gx + gw / 2 - 12, gy + gh - 36, 24)
    text(c, gx + gw / 2, gy + gh - 52, "macOS cannot verify the developer", 7.6, INK, "Helvetica-Bold", "center")
    text(c, gx + gw / 2, gy + gh - 62, "of this file. Are you sure you", 7.6, INK, "Helvetica-Bold", "center")
    text(c, gx + gw / 2, gy + gh - 72, "want to open it?", 7.6, INK, "Helvetica-Bold", "center")
    bw = gw - 28
    button(c, gx + 14, gy + 56, bw, 18, "Open", fill=HexColor("#2F6FEB"), size=8)
    button(c, gx + 14, gy + 32, bw, 18, "Move to Trash", fill=white, color=INK, stroke=LINE, size=8)
    button(c, gx + 14, gy + 8, bw, 18, "Cancel", fill=white, color=INK, stroke=LINE, size=8)
    callout(c, gx + 14, gy + 56, bw, 18, 2)
    arrow(c, 20 + fw + 4, H / 2, gx - 4)


def fig_mac_terminal(c, W, H):
    tw = W - 80
    top = window(c, 40, 14, tw, H - 28, "Terminal", mac=True, bar=HexColor("#E4E4E4"))
    c.setFillColor(HexColor("#1E1E1E"))
    c.rect(41, 15, tw - 2, top - 15, stroke=0, fill=1)
    lines = [
        ("Writing config...", "#D4D4D4"),
        ("Downloading RustDesk 1.4.2...", "#D4D4D4"),
        ("######################################## 100.0%", "#8A8A8A"),
        ("Installing RustDesk.app to /Applications...", "#D4D4D4"),
        ("Re-applying config...", "#D4D4D4"),
        ("", "#D4D4D4"),
        ("RustDesk 1.4.2 installed and configured", "#5BD18B"),
        ("  RustDesk will start automatically on login.", "#D4D4D4"),
        ("Opening RustDesk...", "#D4D4D4"),
    ]
    ly = top - 18
    for s, col in lines:
        text(c, 54, ly, s, 8, HexColor(col), "Courier")
        ly -= 12
    callout(c, 54, top - 18 - 6 * 12 - 3, 196, 11, 3)


def fig_mac_permissions(c, W, H):
    top = window(c, 20, 14, W - 40, H - 28, "System Settings", mac=True, bar=HexColor("#ECECEC"))
    sw = 120
    c.setFillColor(HexColor("#F2F2F4"))
    c.rect(21, 15, sw, top - 15, stroke=0, fill=1)
    for j, item in enumerate(("Wi-Fi", "Bluetooth", "Network", "General", "Privacy & Security", "Desktop & Dock")):
        iy = top - 22 - j * 18
        sel = item == "Privacy & Security"
        if sel:
            rrect(c, 27, iy - 5, sw - 12, 16, 4, fill=HexColor("#2F6FEB"), stroke=None)
        text(c, 34, iy, item, 8, white if sel else INK, "Helvetica-Bold" if sel else "Helvetica")
    px = 21 + sw + 18
    pw = W - 40 - sw - 36
    sections = [("Screen & System Audio Recording", 4), ("Accessibility", 5)]
    sy = top - 22
    for title, n in sections:
        text(c, px, sy, title, 9.5, INK, "Helvetica-Bold")
        text(c, px, sy - 12, "Allow the applications below to use this feature.", 7, MUTED)
        rrect(c, px, sy - 44, pw, 24, 5, fill=white, stroke=LINE)
        app_icon(c, px + 8, sy - 40, 16)
        text(c, px + 32, sy - 35, "RustDesk", 8.5, INK, "Helvetica-Bold")
        toggle(c, px + pw - 38, sy - 39)
        callout(c, px + pw - 38, sy - 39, 26, 14, n)
        sy -= 70


def fig_main_window(c, W, H, app_name):
    ww = W - 60
    top = window(c, 30, 14, ww, H - 28, app_name)
    lw = ww * 0.46
    c.setFillColor(ACCENT_SOFT)
    c.rect(31, 15, lw, top - 15, stroke=0, fill=1)
    text(c, 46, top - 26, "Your Desktop", 12, INK, "Helvetica-Bold")
    text(c, 46, top - 40, "Your desktop can be accessed with this ID and password.", 6.5, MUTED)
    text(c, 46, top - 62, "ID", 7.5, MUTED, "Helvetica-Bold")
    text(c, 46, top - 86, EXAMPLE_ID, 19, INK, "Helvetica-Bold")
    callout(c, 46, top - 91, 118, 22, 1, corner="right")
    text(c, 46, top - 116, "One-time Password", 7.5, MUTED, "Helvetica-Bold")
    text(c, 46, top - 132, "a7k2q9", 12, INK, "Courier-Bold")
    # status line
    c.setFillColor(OK)
    c.circle(50, 28, 3.2, stroke=0, fill=1)
    text(c, 58, 25, "Ready", 8, INK)
    callout(c, 46, 22, 40, 12, 2)
    # technician side, greyed back
    rx = 31 + lw + 20
    text(c, rx, top - 26, "Control Remote Desktop", 10, MUTED, "Helvetica-Bold")
    rrect(c, rx, top - 60, ww - lw - 40, 22, 4, fill=white, stroke=LINE)
    text(c, rx + 8, top - 52, "Enter Remote ID", 8, HexColor("#9AA3B2"))
    button(c, rx, top - 90, 70, 20, "Connect", fill=HexColor("#C9CEDA"), color=white, size=8)
    text(c, rx, top - 112, "Used by technicians. You can ignore this side.", 6.8, MUTED)


def fig_find_app(c, W, H, app_name):
    # desktop shortcut
    app_icon(c, 60, H - 70, 34)
    text(c, 77, H - 84, app_name, 8, INK, "Helvetica-Bold", "center")
    callout(c, 44, H - 92, 66, 58, 1)
    # taskbar
    tb_y = 14
    rrect(c, 20, tb_y, W - 40, 30, 6, fill=HexColor("#E9ECF2"), stroke=LINE)
    rrect(c, 32, tb_y + 7, 16, 16, 3, fill=HexColor("#0067C0"), stroke=None)        # start
    rrect(c, 56, tb_y + 7, 150, 16, 8, fill=white, stroke=LINE)                     # search
    text(c, 66, tb_y + 12, "Search:  " + app_name, 7.5, MUTED)
    callout(c, 56, tb_y + 7, 150, 16, 2)
    # tray: chevron, app icon, wifi, clock
    tx = W - 150
    text(c, tx, tb_y + 11, "^", 9, MUTED, "Helvetica-Bold")
    app_icon(c, tx + 18, tb_y + 7, 16)
    callout(c, tx + 18, tb_y + 7, 16, 16, 3)
    text(c, W - 32, tb_y + 17, "10:42 AM", 7, INK, align="right")
    text(c, W - 32, tb_y + 8, "10/26/2026", 7, INK, align="right")
    text(c, tx - 12, tb_y + 38, "Click ^ to show hidden icons", 7, MUTED, align="center")


def _cm_window(c, x, y, w, h, connected):
    top = window(c, x, y, w, h, "Remote Support")
    c.setFillColor(ACCENT)
    c.circle(x + 30, top - 30, 14, stroke=0, fill=1)
    text(c, x + 30, top - 34.5, "J", 13, white, "Helvetica-Bold", "center")
    text(c, x + 52, top - 26, "Jordan (Technician)", 10, INK, "Helvetica-Bold")
    text(c, x + 52, top - 38, "ID 123 456 789", 7.5, MUTED)
    if connected:
        c.setFillColor(OK)
        c.circle(x + 20, top - 60, 3, stroke=0, fill=1)
        text(c, x + 28, top - 63, "Connected   00:04:12", 8, INK, "Helvetica-Bold")
    else:
        text(c, x + 16, top - 63, "wants to control your desktop", 8, INK)
    text(c, x + 16, top - 78, "Permissions", 7, MUTED, "Helvetica-Bold")
    for j, lab in enumerate(("Keyboard", "Clipboard", "Audio", "Files")):
        px = x + 16 + j * ((w - 32) / 4)
        rrect(c, px, top - 100, (w - 32) / 4 - 6, 18, 4, fill=ACCENT_SOFT, stroke=None)
        text(c, px + ((w - 32) / 4 - 6) / 2, top - 94, lab, 6.8, ACCENT, "Helvetica-Bold", "center")
    return top


def fig_incoming(c, W, H):
    w = 250
    x = (W - w) / 2
    _cm_window(c, x, 14, w, H - 28, connected=False)
    bw = (w - 40) / 2
    button(c, x + 16, 26, bw, 22, "Accept", fill=ACCENT)
    button(c, x + 24 + bw, 26, bw, 22, "Dismiss", fill=white, color=INK, stroke=LINE)
    callout(c, x + 16, 26, bw, 22, 1)
    callout(c, x + 24 + bw, 26, bw, 22, 2)


def fig_in_session(c, W, H):
    w = 250
    x = (W - w) / 2
    _cm_window(c, x, 14, w, H - 28, connected=True)
    button(c, x + 16, 26, w - 32, 22, "Disconnect", fill=DANGER)
    callout(c, x + 16, 26, w - 32, 22, 1)


# ── Page furniture ───────────────────────────────────────────────────────────

class StepHead(Flowable):
    def __init__(self, n, title):
        super().__init__()
        self.n, self.title = n, title

    def wrap(self, aw, _):
        self.width = aw
        return aw, 24

    def draw(self):
        c = self.canv
        c.setFillColor(ACCENT)
        c.circle(11, 9, 11, stroke=0, fill=1)
        text(c, 11, 5, str(self.n), 11, white, "Helvetica-Bold", "center")
        text(c, INDENT, 4, self.title, 13, INK, "Helvetica-Bold")


def step(n, title, body, figure=None, caption=None, heading=None):
    parts = [Paragraph(heading, S["h2"])] if heading else [Spacer(1, 12)]
    parts.append(StepHead(n, title))
    parts += [Paragraph(p, S["step"]) for p in ([body] if isinstance(body, str) else body)]
    if figure is not None:
        parts += [Spacer(1, 8), figure]
        parts.append(Paragraph(caption or "Illustration. Your screen may look slightly different.", S["caption"]))
    return KeepTogether(parts)


def box(rows, fill=ACCENT_SOFT, width=CONTENT_W):
    t = Table([[r] for r in rows], colWidths=[width])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), fill),
        ("LEFTPADDING", (0, 0), (-1, -1), 14), ("RIGHTPADDING", (0, 0), (-1, -1), 14),
        ("TOPPADDING", (0, 0), (0, 0), 12), ("BOTTOMPADDING", (0, -1), (-1, -1), 12),
        ("TOPPADDING", (0, 1), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -2), 2),
    ]))
    return t


def contact_box(opts, width=CONTENT_W):
    def field(label, value):
        v = value if value else "_" * (46 if width == CONTENT_W else 26)
        return Paragraph(f"<b>{label}</b>&nbsp;&nbsp;&nbsp;{v}", S["cell"])
    rows = [Paragraph("<b>Your IT support contact</b>", S["body"])]
    if not opts.company:
        rows.append(field("Company", ""))
    rows += [field("Phone", opts.phone), field("Email", opts.email)]
    return box(rows, width=width)


def side_by_side(left, right, gap=10):
    t = Table([[left, right]], colWidths=[(CONTENT_W + gap) / 2] * 2)
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                           ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (0, 0), gap),
                           ("RIGHTPADDING", (1, 0), (1, 0), 0),
                           ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
    return t


def trouble_table(rows):
    data = [[Paragraph("If this happens", S["cellb"]), Paragraph("Try this", S["cellb"])]]
    data += [[Paragraph(a, S["cell"]), Paragraph(b, S["cell"])] for a, b in rows]
    t = Table(data, colWidths=[CONTENT_W * 0.36, CONTENT_W * 0.64])
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, -1), 0.6, LINE),
        ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 10),
    ]))
    return t


def build(path, doc_title, story, opts):
    provider = opts.company or "Your IT support provider"
    contact = " · ".join(x for x in (opts.phone, opts.email) if x)

    def on_page(c, doc):
        c.saveState()
        c.setFillColor(ACCENT)
        c.rect(0, PAGE_H - 8, PAGE_W, 8, stroke=0, fill=1)
        text(c, MARGIN, PAGE_H - 30, provider.upper(), 7.5, MUTED, "Helvetica-Bold")
        text(c, PAGE_W - MARGIN, PAGE_H - 30, doc_title, 7.5, MUTED, align="right")
        c.setStrokeColor(LINE)
        c.line(MARGIN, 0.6 * inch, PAGE_W - MARGIN, 0.6 * inch)
        foot = "Need help? Contact " + (provider if provider != "Your IT support provider" else "your IT support provider")
        if contact:
            foot += ": " + contact
        text(c, MARGIN, 0.42 * inch, foot, 8, MUTED)
        text(c, PAGE_W - MARGIN, 0.42 * inch, f"Page {doc.page}", 8, MUTED, align="right")
        c.restoreState()

    doc = BaseDocTemplate(str(path), pagesize=letter, title=doc_title,
                          author=opts.company or "IT support", subject="Remote Support client guide",
                          leftMargin=MARGIN, rightMargin=MARGIN, topMargin=0.85 * inch, bottomMargin=0.85 * inch)
    frame = Frame(MARGIN, 0.85 * inch, CONTENT_W, PAGE_H - 1.7 * inch, id="f", leftPadding=0,
                  rightPadding=0, topPadding=0, bottomPadding=0)
    doc.addPageTemplates([PageTemplate(id="p", frames=[frame], onPage=on_page)])
    doc.build(story)


# ── The two guides ───────────────────────────────────────────────────────────

def install_guide(opts):
    app = opts.app_name
    host = opts.host or "your-support-server"
    st = [
        Paragraph(f"Installing {app}", S["title"]),
        Paragraph(f"{app} is a small program that lets your IT support team see your screen and fix "
                  "problems for you, without a visit. Installing it takes about two minutes.", S["lede"]),
        Spacer(1, 14),
        side_by_side(
            box([
                Paragraph("<b>Before you start</b>", S["body"]),
                Paragraph("• Your IT support provider emails you a download link. "
                          "Only install from a link they sent you.", S["cell"]),
                Paragraph("• You may be asked for the password you use to sign in to this computer.", S["cell"]),
                Paragraph("• You only need to do this once per computer.", S["cell"]),
            ], width=(CONTENT_W - 10) / 2),
            contact_box(opts, width=(CONTENT_W - 10) / 2),
        ),

        step(1, "Open the link and download the installer",
             "Click the link in the email. The page picks the right version for your computer and marks it "
             "<b>Recommended</b>. Click it to download.",
             Figure(228, fig_download_page, host=host), heading="On a Windows PC"),
        step(2, "Open the downloaded file",
             ["Open your <b>Downloads</b> folder, or click the file at the bottom or top of your browser.",
              "If a blue <b>Windows protected your PC</b> window appears, click <b>More info</b> (1), "
              "then <b>Run anyway</b> (2). This is normal for programs your IT provider builds for you."],
             Figure(178, fig_smartscreen)),
        step(3, "Allow the app to make changes",
             "Windows asks whether to allow the app to make changes to your device. Click <b>Yes</b> (3). "
             "The installation then finishes on its own, usually in under a minute.",
             Figure(178, fig_uac)),
        step(4, "You're done",
             f"A <b>{app}</b> shortcut appears on your desktop and the app starts with Windows. There is "
             "nothing to set up. Your IT support team can now see this computer in their list."),

        step(1, "Download the installer",
             "Click the link in the email and choose <b>macOS</b>. A file ending in <b>.command</b> "
             "is saved to your Downloads folder.", heading="On a Mac"),
        step(2, "Open it with a right-click",
             ["In Finder, open <b>Downloads</b>. Right-click (or Control-click) the file and choose "
              "<b>Open</b> (1). When the warning appears, click <b>Open</b> (2).",
              "On macOS 15 or later, if there is no Open button: click <b>Done</b>, open "
              "<b>System Settings → Privacy &amp; Security</b>, scroll down and click <b>Open Anyway</b>."],
             Figure(200, fig_mac_open)),
        step(3, "Let the installer run",
             "A Terminal window opens and shows its progress. When you see <b>installed and configured</b> (3), "
             "the app opens by itself and you can close the Terminal window.",
             Figure(180, fig_mac_terminal)),
        step(4, "Allow screen sharing and control",
             ["The first time it runs, macOS asks to let RustDesk record the screen and control the computer. "
              "Without these, your technician sees a blank screen or can't move the mouse.",
              "Open <b>System Settings → Privacy &amp; Security</b>. Turn on <b>RustDesk</b> under "
              "<b>Screen &amp; System Audio Recording</b> (4) and under <b>Accessibility</b> (5). "
              "If macOS asks to quit and reopen RustDesk, click <b>Quit &amp; Reopen</b>."],
             Figure(185, fig_mac_permissions)),

        KeepTogether([
            Paragraph("On Linux", S["h2"]),
            Paragraph("Download the Linux installer from the link, open a terminal in your Downloads folder and run "
                      "<font face='Courier'>sudo bash ./</font><i>file-name</i><font face='Courier'>.sh</font>, "
                      "using the name of the file you downloaded. Enter your password when asked.", S["body"]),
        ]),

        Paragraph("If something goes wrong", S["h2"]),
        trouble_table([
            ("The link says it is no longer available",
             "Links can expire or be used a limited number of times. Ask your IT support provider for a new one."),
            ("Windows says the app is blocked and there is no Run anyway button",
             "Your computer's security settings don't allow new apps. Contact your IT support provider."),
            ("The Mac says the file can't be opened",
             "Use right-click → Open instead of double-clicking, or use Open Anyway in Privacy &amp; Security (step 2)."),
            ("The technician sees a black screen on a Mac",
             "Screen recording isn't allowed yet. Repeat step 4, then quit and reopen RustDesk."),
        ]),
    ]
    build(opts.out / "install-remote-support.pdf", f"Installing {app}", st, opts)


def help_guide(opts):
    app = opts.app_name
    st = [
        Paragraph(f"Getting help with {app}", S["title"]),
        Paragraph(f"What to do when you call for help and your technician connects to your computer with "
                  f"{app}. You stay in control the whole time.", S["lede"]),
        Spacer(1, 14),
        contact_box(opts),

        step(1, "Open the app",
             f"Double-click the <b>{app}</b> shortcut on your desktop (1), or type <b>{app}</b> into the Windows "
             "search box (2). It may also be running already: look for its icon near the clock (3). "
             "On a Mac, open <b>RustDesk</b> from Applications.",
             Figure(150, fig_find_app, app_name=app)),
        step(2, "Read your ID to your technician",
             ["Your <b>ID</b> is the 9-digit number under <b>Your Desktop</b> (1). Read it out when your "
              "technician asks for it. It never changes for this computer, so you can write it down here:",
              "<b>My ID:</b>&nbsp;&nbsp;___ ___ ___&nbsp;&nbsp;___ ___ ___&nbsp;&nbsp;___ ___ ___",
              "Check that the bottom corner says <b>Ready</b> with a green dot (2). If it says anything else, "
              "check that the computer is connected to the internet."],
             Figure(222, fig_main_window, app_name=app),
             caption=f"Illustration with an example ID ({EXAMPLE_ID}). Your screen may look slightly different."),
        step(3, "Accept the connection",
             ["A small window shows who is connecting. If you are expecting them, click <b>Accept</b> (1). "
              "If you don't recognise the name, or nobody told you to expect a connection, click "
              "<b>Dismiss</b> (2) and call your IT support provider.",
              "Your IT provider may have set up this computer so technicians can connect without you clicking "
              "Accept, for example to fix it after hours. You still see this window for as long as they are "
              "connected."],
             Figure(196, fig_incoming)),
        step(4, "During the session",
             ["You see everything the technician does: the mouse moves on its own and windows open and close. "
              "Try not to use the mouse or keyboard at the same time unless they ask you to.",
              "Your technician will never ask you to read out a password over the phone. If they need you to "
              "type one, type it yourself."]),
        step(5, "End the session",
             "Your technician disconnects when they're done. You can end it yourself at any time by clicking "
             "<b>Disconnect</b> (1) in the small window. Closing that window also ends the session.",
             Figure(196, fig_in_session)),

        Paragraph("Your privacy and control", S["h2"]),
        box([
            Paragraph("• Only your IT support provider's technicians can connect, and only to computers where "
                      "their version of the app is installed.", S["cell"]),
            Paragraph("• Every connection is logged on their side: who connected, when, and for how long.", S["cell"]),
            Paragraph("• While someone is connected, the session window stays on your screen. "
                      "Disconnect ends it immediately.", S["cell"]),
            Paragraph("• To remove the app, contact your IT support provider first, because they use it to look "
                      "after this computer.", S["cell"]),
        ]),

        Paragraph("If something goes wrong", S["h2"]),
        trouble_table([
            ("I can't find the app",
             f"Search for {app} or RustDesk in the Windows search box, or in Applications on a Mac."),
            ("My technician can't connect",
             "The computer must be switched on, awake and online. Make sure it isn't asleep or shut down."),
            ("The screen goes black or the mouse doesn't move on a Mac",
             "Screen recording or Accessibility permission is off. See step 4 of the installation guide."),
        ]),
    ]
    build(opts.out / "getting-help.pdf", f"Getting help with {app}", st, opts)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--company", default="", help="your company name, printed in the header and footer")
    ap.add_argument("--phone", default="", help="support phone number")
    ap.add_argument("--email", default="", help="support email address")
    ap.add_argument("--host", default="", help="dashboard hostname shown in the download-page picture")
    ap.add_argument("--app-name", default="Remote Support",
                    help="the app name clients see (a client group's branded name)")
    ap.add_argument("--out", type=Path, default=Path(__file__).resolve().parent, help="output folder")
    opts = ap.parse_args()
    opts.out.mkdir(parents=True, exist_ok=True)
    install_guide(opts)
    help_guide(opts)
    print(f"Wrote {opts.out / 'install-remote-support.pdf'}")
    print(f"Wrote {opts.out / 'getting-help.pdf'}")


if __name__ == "__main__":
    main()
