"""Resolve the multi-source ``reference_ranges`` structure.

Each marker in ``bloodwork_data.yaml`` looks like::

    Vitamin D:
      unit: ng/mL
      active: curated            # source that drives traffic-light coloring
      sources:
        curated:       {low: 20,   high: 50,  optimal_low: 40, optimal_high: 60}
        insidetracker: {low: 30,   high: 100, optimal_low: 32, optimal_high: 100}
        lab:           {low: 30,   high: 100, optimal_low: null, optimal_high: null}

``resolve_active`` flattens the active source into the legacy
``{low, high, optimal_low, optimal_high, unit}`` shape the generators expect,
so downstream status/plot logic needs no changes.  ``sources`` is preserved on
the resolved dict for anything that wants to show alternates.
"""

_FIELDS = ("low", "high", "optimal_low", "optimal_high")


def resolve_active(raw: dict) -> dict:
    """Map {marker: multi-source entry} -> {marker: flat active range}.

    Also accepts entries already in the flat legacy shape (no ``sources`` key)
    and passes them through unchanged, so the loader is safe either way.
    """
    out = {}
    for name, entry in raw.items():
        if not isinstance(entry, dict) or "sources" not in entry:
            out[name] = entry  # already flat / legacy
            continue
        sources = entry.get("sources") or {}
        active = entry.get("active")
        if active not in sources:
            # fall back deterministically: curated > insidetracker > lab > any
            active = next((s for s in ("curated", "insidetracker", "lab") if s in sources),
                          next(iter(sources), None))
        band = dict(sources.get(active, {})) if active else {}
        flat = {f: band.get(f) for f in _FIELDS}
        flat["unit"] = entry.get("unit", "")
        flat["active_source"] = active
        flat["sources"] = sources
        out[name] = flat
    return out
