# Third-party notices

Agenda is licensed under GPL-3.0-or-later (see [LICENSE](LICENSE)). It
includes or bundles the third-party components below, each under its own
license, and all of those licenses are compatible with the GPL.

## Vendored in this repository

| Component | Version | License | Location |
|---|---|---|---|
| [SortableJS](https://github.com/SortableJS/Sortable) | 1.15.2 | MIT | `static/vendor/Sortable.min.js` |
| [marked](https://github.com/markedjs/marked) | 15.0.12 | MIT | `static/vendor/marked.min.js` |

## Fetched at build time and bundled in the macOS app

| Component | License | How it's fetched |
|---|---|---|
| [Sparkle](https://github.com/sparkle-project/Sparkle) 2.9.5 | MIT (plus licenses of its bundled components) | Swift Package Manager (`mac/Agenda/Package.resolved`) |
| [CPython](https://www.python.org/) 3.13 via [python-build-standalone](https://github.com/astral-sh/python-build-standalone) | PSF License Agreement (plus licenses of its bundled libraries) | `mac/Agenda/scripts/vendor-python.sh` |
| [FastAPI](https://github.com/fastapi/fastapi) | MIT | `requirements.txt` |
| [Starlette](https://github.com/encode/starlette) | BSD-3-Clause | dependency of FastAPI |
| [Pydantic](https://github.com/pydantic/pydantic) | MIT | `requirements.txt` |
| [Uvicorn](https://github.com/encode/uvicorn) and its `[standard]` extras | BSD-3-Clause (extras: MIT, BSD, Apache-2.0) | `requirements.txt` |

Exact Python package versions are pinned when the app is built. Each
package's license file ships inside the bundled `site-packages`.
