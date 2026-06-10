# Legacy GUIs

The files here are older, single-purpose browser GUIs that predate
`src/go2_local_brain/ai_autonomy_gui.py`. They still work, but the
autonomy GUI is the canonical control surface — it supersedes both:

- `gui.py` — original unified GUI (manual + AI + video). Subset of
  what `ai_autonomy_gui.py` does now.
- `control_gui.py` — minimal video + keyboard + sport-action cockpit.
  Subset of `ai_autonomy_gui.py`'s manual override panel.

## Why keep them at all?

If you want a small fast-loading page (the autonomy GUI is ~1500 lines
of HTML/JS) for a tablet or a phone, the old single-purpose GUIs are
still handy. They're frozen — no new features land here.

## Running them

```bash
python -m go2_local_brain.legacy.gui
python -m go2_local_brain.legacy.control_gui
```

If those fail to import after the move, that means a downstream module
still references the old paths — see `docs/migration.md` or just port
the call site to `ai_autonomy_gui`.
