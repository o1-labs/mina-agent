"""ANSI (16 / 256 / truecolor) SGR escape sequences -> HTML spans.

difftastic's structural highlighting is expressed in ANSI colour; converting
it (rather than stripping it) preserves the changed-subexpression highlighting
that is the point of a tree-sitter diff. Dependency-free.
"""
import html
import re

SGR = re.compile("\x1b\\[([0-9;]*)m")
# 16-colour palette tuned for a dark background
BASE = ["#000000", "#c0392b", "#27ae60", "#b8860b", "#2980b9", "#8e44ad", "#16a085", "#bdc3c7"]
BRIGHT = ["#7f8c8d", "#e74c3c", "#2ecc71", "#f1c40f", "#3498db", "#9b59b6", "#1abc9c", "#ecf0f1"]


def _c256(n):
    if n < 16:
        return (BASE + BRIGHT)[n]
    if n < 232:
        n -= 16
        r, g, b = n // 36, (n % 36) // 6, n % 6
        f = lambda v: 55 + v * 40 if v else 0
        return f"#{f(r):02x}{f(g):02x}{f(b):02x}"
    v = 8 + (n - 232) * 10
    return f"#{v:02x}{v:02x}{v:02x}"


def to_html(text):
    """ANSI text -> HTML (escaped, with <span style> for colour/weight)."""
    out, fg, bg, bold, dim = [], None, None, False, False
    open_span = False

    def style():
        s = []
        if fg:
            s.append(f"color:{fg}")
        if bg:
            s.append(f"background:{bg}")
        if bold:
            s.append("font-weight:bold")
        if dim:
            s.append("opacity:.7")
        return ";".join(s)

    def flush():
        nonlocal open_span
        if open_span:
            out.append("</span>")
            open_span = False

    pos = 0
    for m in SGR.finditer(text):
        out.append(html.escape(text[pos:m.start()]))
        pos = m.end()
        codes = [int(x) for x in m.group(1).split(";") if x != ""] or [0]
        i = 0
        while i < len(codes):
            c = codes[i]
            if c == 0:
                fg = bg = None; bold = dim = False
            elif c == 1:
                bold = True
            elif c == 2:
                dim = True
            elif c == 22:
                bold = dim = False
            elif c == 39:
                fg = None
            elif c == 49:
                bg = None
            elif 30 <= c <= 37:
                fg = BASE[c - 30]
            elif 90 <= c <= 97:
                fg = BRIGHT[c - 90]
            elif 40 <= c <= 47:
                bg = BASE[c - 40]
            elif 100 <= c <= 107:
                bg = BRIGHT[c - 100]
            elif c in (38, 48):
                mode = codes[i + 1] if i + 1 < len(codes) else None
                col = None
                if mode == 5:
                    col = _c256(codes[i + 2]); i += 2
                elif mode == 2:
                    col = f"#{codes[i+2]:02x}{codes[i+3]:02x}{codes[i+4]:02x}"; i += 4
                if c == 38:
                    fg = col
                else:
                    bg = col
            i += 1
        flush()
        st = style()
        if st:
            out.append(f'<span style="{st}">')
            open_span = True
    out.append(html.escape(text[pos:]))
    flush()
    return "".join(out)
