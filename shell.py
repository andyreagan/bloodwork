"""Shared page shell for the four generated pages (index, fitness,
correlations, models): the <head> that pulls in the dashboard kit
(ar.css / ar.js, vendored from ~/projects/2026/dashboard-kit) and the sidebar
that links the pages together.  Plain strings — callers drop them into their
own templates."""

PAGES = [
    ("index.html",        "🩸", "Bloodwork"),
    ("fitness.html",      "📊", "Fitness Sources"),
    ("correlations.html", "🔬", "Correlations"),
    ("models.html",       "📐", "Models"),
]


def head(title: str, generated: str, extra: str = "") -> str:
    """Everything from <!DOCTYPE> through </head>.  `extra` is page-local
    <style>/<script> markup appended inside <head>."""
    return f"""<!DOCTYPE html>
<html lang="en" data-site="health">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="ar-repo" content="bloodwork">
<meta name="ar-generated" content="{generated}">
<title>{title}</title>
<link rel="stylesheet" href="ar.css">
<script src="https://cdn.jsdelivr.net/npm/d3@7.9.0/dist/d3.min.js"></script>
<script src="ar.js"></script>
{extra}
</head>
"""


def sidebar(active: str, sections: str = "") -> str:
    """The <nav class="sidebar">.  `active` is the current page's filename;
    `sections` is extra .nav-section markup for in-page navigation."""
    items = "\n".join(
        f'    <a class="nav-item{" active" if href == active else ""}" href="{href}">'
        f'<span class="icon">{icon}</span> {label}</a>'
        for href, icon, label in PAGES
    )
    return f"""<nav class="sidebar">
  <div class="sidebar-logo">Andy's <span>Health</span></div>
  <div class="nav-section">
    <div class="nav-section-title">Pages</div>
{items}
  </div>
{sections}
</nav>
"""
