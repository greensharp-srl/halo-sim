# halo-sim

Phone-backed simulator for Brilliant Labs Halo glasses. See README.md for architecture and usage.

- Python env lives outside the repo: `~/.venvs/halo-sim` (the `./halo-sim` launcher sets `UV_PROJECT_ENVIRONMENT`).
- Commands: `./halo-sim setup | serve | apps | install <folder|git> [--link] | uninstall <id> | run app.py | repl | test`.
- Apps are separate projects with a `halo-app.toml`; installed copies in `~/.halo-sim/apps`, venvs in `~/.halo-sim/venvs`. The sim shows a launcher menu (tap = next, double tap = open, phone HOME = back).
- Runtime data: glasses filesystem `~/.halo-sim/sandbox`, phone HTTPS certs `~/.halo-sim/certs`.
- Run `./halo-sim test` before committing. Built-in starter app: `apps/halo_demo/`. App-specific code lives in the app's own repo, not here.
- Git: remote `origin` = github.com/greensharp-srl/halo-sim, default branch `main`.
  Work on feature branches and open PRs with `gh pr create` for anything non-trivial; small fixes may go straight to `main`.
