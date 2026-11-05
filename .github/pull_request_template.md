## What does this change?


## Why?

<!-- Link the issue if there is one: "Closes #123" -->

## How was it tested?

- [ ] `PYTHONPATH=. python -m unittest discover -s tests`
- [ ] `swift test` in `mac/Agenda` and/or `apple/AgendaSyncCore` (if Swift changed)
- [ ] Tried it in the running app (`mac/Agenda/install.sh` or the dev server)
- [ ] Screenshots for UI changes (light and dark mode)

## Checklist

- [ ] No personal paths, hostnames, project names, or credentials (`scripts/verify-public.sh` passes)
- [ ] Schema changes, if any, are additive and idempotent migrations in `db.py`
- [ ] No new network requests, or they're documented and a no-op when unused
