# Contributing to termg

Thanks for taking a look. termg is deliberately a **single file** (`termg.py`)
with **no third-party Python dependencies** — just the system GTK 3 / VTE 2.91
bindings. Please keep it that way unless there's a strong reason not to.

## Getting set up

Install the runtime dependencies for your distro (see the README), then run it
straight from the checkout:

```bash
python3 termg.py
```

No build step, no virtualenv required.

## Coding guidelines

- **One file, no new dependencies.** If you reach for a PyPI package, open an
  issue first to discuss it.
- **Match the existing style.** 4-space indent, lines kept reasonably short.
  The code is `pyflakes`-clean and the CI checks that — run it locally:
  ```bash
  python3 -m pyflakes termg.py
  python3 -m py_compile termg.py
  ```
- **Comment the *why*, not the *what*.** The hard parts here are GTK/VTE
  quirks; a one-line note explaining *why* something is done a certain way is
  worth far more than restating the code. Public methods and both classes have
  docstrings — keep that up for anything new and non-trivial.
- **Theme everything via the `.tt-root` CSS** in `_apply_chrome_theme()` so the
  app keeps styling itself regardless of the user's system theme; don't rely on
  inherited GTK theme colours.

## Testing

There's no automated UI test suite. Please test changes manually on a real
session, and mention in your PR what you checked (which distro, X11 or Wayland,
tabbed and tiled, light and dark). For headless smoke-testing you can drive the
app under `xvfb-run` and render window regions to PNGs, but that's optional.

## Reporting bugs / requesting features

Use the issue templates. For bugs, the most useful things are your distro and
desktop, X11 vs Wayland, and exact steps to reproduce. Screenshots help a lot.

## Licence

By contributing, you agree that your contributions are licensed under the
project's MIT licence.
