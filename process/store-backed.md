# Store-backed declaration

This workspace's process state lives in the ModernPath store, not in file
ledgers. Every process write goes through `modernpath author` / `working-set`
/ `process` / `factory` verbs; the retired ledger files are not recreated.

- **Declared:** 2026-09-14
- **Store:** https://api.workload.test-plat.modernpath.ai · system 77 (physiological-sensing)
- **Source:** USER:2026-09-14:section G run — the human sanctioned writing this marker by hand for the system under test, because a workspace born on the store has no `migrate flip` path to it (BACKLOG-TOOL-1)
- **Retired:** none — this workspace never held file ledgers (`tasks/*-REQUIREMENTS.md`, `WORKLIST.md`, `epics/*`, `process/releases.md` were never created)
