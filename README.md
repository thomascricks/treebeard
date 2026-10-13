# Treebeard 1.0.1 — public edition (research prototype)

Treebeard is a research prototype of a **governed multi-agent system** built
around a language-model **router**. Several agents, each a separate Unix
account with its own scheduled job, share one routing library. The library
decides which model tier serves each request and records every decision. A
governance layer sits around the agents: a constitution and charters, policy
files that fail closed, root-gated commissioning of work, hash-chained
receipts and an optional containment layer for generated code.

This repository is published as evidence of engineering, governance and
evaluation practice. It is **not a product**: read the status table and the
known deficiencies before running it. Claims below were checked against the
shipped code or a recorded test run; where a run was made on an earlier tree,
the text says which. Where something was not tested, the text says so.

This tree is exported from a private production system by a fail-closed
exporter. Provider and model identities are replaced by tier aliases (for
example `free_v1`, `cheap_v1`). The local tier names the open model
`phi3:mini` as itself. The export passed the exporter's publication gates
with 0 findings. Those gates do not prove that no operator data could
exist anywhere.

## Status of this release

**The release tree.** It is derived from the certified RC6 tree, with
changes that were each approved by an operator ruling and verified after
installation. The resulting tree has not had a full acceptance run.

- `treebeard/ask.py`: exit status, stderr diagnostics, and comment wording;
- `shared/lib/sms.py`: re-running the installer no longer adds a duplicate
  `init` event;
- five files whose exported text changed because the local tier keeps its
  real model name, `phi3:mini`: `router.py`, `pricing.py`, `telemetry.py`,
  `meeseeks.py`, `hello.py`;
- `LICENSE` (GPL-3.0-only);
- `shared/lib/career_packets.py` and its test suite (résumé output
  validation, see Known deficiencies);
- fixture and comment hygiene in `sun/agent.py`, `sprint1_regression.py`, the
  two containment scripts, two Clark modules and `providers.env.example` (no
  behaviour change);
- the publication manifest;
- this README.

**What the measurements cover.**
- The clean-room acceptance runs below were made on the RC6 tree.
- The `phi3:mini` change was re-tested in a separate acceptance VM.
- The `sms.py` change was re-tested in a separate installer VM.
- No full acceptance run has been made on this exact tree.

Rows report RC6 results unless stated. Checks made on this exact tree: the
harness (44/0/0) and the publication sweep. The 23-row gate-2 plan (operator)
and the 6-row maintainer re-run were made on v1.0's code.

| area | status |
|---|---|
| Install (all phases) and preflight on a fresh host | verified in clean-room acceptance (container and VM) |
| Regression harness | verified: 44 of 44 suites pass in a container without systemd, and on a systemd host once timers are enabled. 42 of 44 pass on a systemd host while timers are disabled, the installer's default (see Verify). |
| Containment: provisioning, `--check`, rollback, sudo argv matrix | verified: provisioning test suite on three platforms, plus the acceptance VM |
| The jail | verified (denial probe in the acceptance VM) |
| Commissioning with `tb-commission` | verified in the acceptance VM, without a model |
| Local tier (`phi3:mini` through Ollama) | verified for **ungoverned** short calls in an acceptance VM. Governed agent turns cannot use it (see Model tiers). |
| Hosted tiers, governed turns | exercised in clean-room acceptance only against a **mock** provider on loopback. No real provider was called from a clean install. |
| The six legs end to end | verified only on the private production system (one run, 2026-10-07). Not demonstrated from a clean install of this edition. |
| Scheduled jobs (timers) | off by default. Enabling them was verified in one acceptance boot. Timer firing over time is not tested. |
| Non-default `TB_BASE` / `TB_ETC` | not supported, not tested |
| Frontier adapter | not shipped, not tested |

## What Treebeard is, in plain terms

You ask an agent something. The router classifies the request, picks the
cheapest eligible tier (for governed turns the local tier is skipped by
design; see Model tiers), and escalates up a ladder of tiers if the answer
fails. Every choice is written to a telemetry store, together with why.
Agents also run unattended on timers:
- a daily digest;
- a weekly backup;
- career-search jobs;
- a reflector that carries work between the task board and the code-generation
  pipeline.

What an agent may do is constrained by text it must load (the constitution
and its charter) and by policy files that deny by default. The work it may
start is constrained by root-gated commissioning steps that write receipts.

## Architecture (as shipped)

- **Router** (`shared/lib/router.py`). It classifies a request and ranks the
  eligible tiers on cost, health and measured performance; each agent's
  personality profile breaks near-ties. Each decision is recorded
  (`shared/lib/decisions.py`), and a recorded decision can be reconstructed
  together with the model calls it produced.
- **Agents** are per-Unix-user identities. `treebeard/ask.py` runs one
  governed turn. It loads the agent's identity from
  `/etc/treebeard/<agent>.charter.txt`; this edition ships only
  `treebeard.charter.txt`. An agent without a charter runs **unchartered**:
  it is told it has none and is bound by the constitution alone, and `ask.py`
  prints a `WARN` line on stderr. `treebeard/meet.py` convenes several agents
  around a topic.
- **Missions and charters are separate.** The charter is the agent's standing
  identity, a root-owned file. Missions are units of assigned work in a
  durable store (`shared/lib/missions.py`), selected per turn. An explicit
  operator assignment that cannot be honoured fails closed instead of
  silently running without it.
- **Memory tiers:**
  - short-term: a day-bucketed record of every exchange;
  - subconscious: self-flagged fragments in a per-agent vector store;
  - long-term: a per-agent consolidated store, where writes are own-only
    (an overseer may read across agents, never write);
  - a daily "dream" pass, driven by Sun, and a weekly consolidation by Moon.

  The dream and long-term steps are among the open defects on a fresh install
  (see Known deficiencies).
- **Capability is policy-governed.** Agents reach each other through
  store-and-forward messaging (`shared/lib/sms.py`), and cause work through
  deferred dispatch (`shared/lib/dispatch.py`). Both read root-owned policy
  files under `/etc/treebeard` and permit nothing when those are absent.
- **Verification** is one harness, `shared/lib/regression.py`, that runs 44
  registered module suites in its default (safe) mode.

Not in this edition: the autonomous task loop and scheduler, and the
publication tooling that produced this tree.

## Governance model

- **Authority.**
  - Every governed turn loads `etc/treebeard/constitution.txt` first. If it
    cannot be loaded, the turn stops with `FATAL: Constitution unreadable: <reason>`
    on stderr and exit 1.
  - The constitution and the Treebeard charter are installed `root:root 644`,
    and preflight fails until they are.
- **Approvals.**
  - Work enters as a card on the task board. It moves to execution only
    through `tb-commission`, run as root. That step authorizes the card,
    creates its project and links the two, writing a receipt at each step.
  - Generated code needs a second, human approval (Gate 2) before it is
    deposited for use.
  - This edition has no separate submit or approve command.
- **Protected boundaries.**
  - Messaging, dispatch and the board writer are governed by root-owned
    policy files. A missing, empty or malformed policy refuses the action
    and is audited as `policy-error`.
  - Once containment is provisioned, the service account cannot write the
    containment code, `tb-commission` or its policy.
- **Receipts.**
  - Commissioning writes hash-chained receipts (`shared/ctl/tbreceipt.py`)
    to a root-owned file. Its integrity is *checked* (append-only against a
    root-held baseline), not enforced by the filesystem.
  - Receipts of executed dispatch work are injected into the agent's next
    prompt and printed after its reply, so the agent is shown what actually
    ran. This is implemented and covered by its suites; it has not been
    demonstrated end to end on a clean install.
- **Recorded, not authenticated.** The operator's only login is the service
  account, so commissioning is *recorded* and root-gated. It is not proof of
  who the operator was. Operator authentication is deferred beyond 1.0.
- **Containment, as it stands.**
  - The containment layer ships but is **not** provisioned by the installer.
  - When provisioned, generated code runs as a separate jail account through
    exactly one sudo rule (see Containment). The claim is
    **runtime-contained, not operator-authenticated**.
  - On the private production system an equivalent containment layer was
    deployed and accepted on 2026-10-05.

## Agent orchestration (as shipped)

The installer renders these systemd units. It enables **no** timer until the
operator asks (see Timers).

| unit | runs as | what it does |
|---|---|---|
| `treebeard-agent@sun` (daily timer) | sun | status digest email with agent reports; drives the daily dream pass |
| `treebeard-agent@moon` (weekly timer) | moon | configuration backup; weekly memory consolidation |
| `treebeard-reflector` (timer) | treebeard | carries state between the task board and Meeseeks |
| `treebeard-career` (daily timer) | treebeard | the career hunter (agent `han`); its search stage runs in the rootless Podman toolroom |
| `treebeard-clark-intake` (15-minute timer) | treebeard | the `clark` agent's email intake |
| `treebeard-clark-patrol` (timer) | treebeard | the `clark` agent's employer patrol; it only adds to a queue |

- **Meeseeks** (`shared/meeseeks/`) is the code-generation pipeline: draft,
  jailed tests, a three-step review, then Gate 2. Each stage runs on exactly
  the tier it is pinned to, with no fallback. When that tier refuses, the
  section is blocked.
- **Anvil** (`shared/anvil/`) is the intake and human-lock gate for approved
  code. Build and test of candidates are **not implemented** in this edition.
- `han` and `clark` have no shipped charter, so they run unchartered.

## Testing and evaluation

- **The regression harness** (`shared/lib/regression.py`) runs 44 registered
  safe suites, plus 6 live suites that are opt-in. Results on fresh installs
  are under Verify.
- **Optimized-mode enforcement** (`shared/lib/optmode.py`).
  - Under `python -O`, assertions vanish. A selftest built on bare asserts
    would then exit 0 and record a false PASS. Three suites were found doing
    exactly that and were repaired.
  - Every registered suite must now declare its `-O` contract, and an
    undeclared suite is an integrity failure. Suites that depend on asserts
    must refuse to run under `-O`, with exit code 2 and a diagnostic, and the
    harness checks that they do.
- **Clean-room certification history.** Release candidates are frozen,
  exported and installed into disposable containers and VMs, then scored.
  Failures are recorded, not relabelled.
  - An earlier candidate (RC3) was clean-room certified.
  - RC5 was **refuted** by its clean-room run.
  - RC6 passed its container certification with 0 defects (harness 44/0/0)
    and its provisioned-VM row with 0 defects and 3 recorded environment
    differences. The full acceptance suite, reported next, found 21
    falsified rows; the 0-defect figures cover the narrower certification
    scope.
  - A full acceptance suite was then run on RC6: 120 capability rows,
    each test in a fresh VM, scored into nine classes.

  | RC6 acceptance class | rows |
  |---|---|
  | verified functional | 36 |
  | simulated-only | 41 |
  | **falsified** | 21 |
  | incomplete | 10 |
  | not shipped | 6 |
  | other (evidence-accepted, blocked, inconclusive) | 6 |

  Its 31 failures (drawn from the falsified, incomplete and other classes)
  are the source of the defect register summarized under Known deficiencies.
- **Independent review.** An externally designed test plan (69 rows) was run
  against the documentation in clean-room VMs. It found 4 documentation
  defects, all predicted before execution, and they were corrected.
- A second externally designed plan (175 rows) was written from this README
  alone. 23 core rows were run in a fresh VM by the maintainer and again by
  the operator; predictions were filed before the maintainer's runs. It found
  6 documentation defects, all corrected.
- **Mutation testing**, with small hand-built mutant sets, was used where a
  fix or a safety property had to be shown to be load-bearing:
  - the containment proof suites: deliberately broken copies of the writer,
    the reflector gate and the provisioning scripts, each killed by a named
    test;
  - the alias-map change (6 of 6 mutants rejected);
  - the `ask.py` stderr change (5 of 5 killed);
  - the `sms.py` installer fix (5 of 5 killed).

## Demonstrated operation (only what the evidence shows)

- **One governed end-to-end run, on the private production system
  (2026-10-07).** An operator request became a board card (card 63). It was
  routed, commissioned as root, executed by Meeseeks, approved at Gate 2 and
  marked done by the reflector.
  - Execution failed at first: the free tier refused every call (HTTP 429)
    and the section was blocked, fail-closed.
  - It passed after the operator re-pinned Meeseeks to the cheap tier. The
    generated code's tests then ran in the jail, 15 of 15 passing, through
    the single sudo rule.
  - This was one run, with recorded deviations and human steps at
    commissioning and Gate 2.
- **Capability ceiling (Sprint 3).** An autonomous-development test asked
  Meeseeks to build a chess component against a locked test suite. It
  **failed, 5 of 8**, and was closed as a capability-ceiling finding, not
  relabelled as a pass. A later review of that run noted the failure may
  have been non-convergence on a small bug rather than a hard model limit.
  Either way, unattended development of non-trivial code was **not**
  achieved.
- **Not demonstrated:**
  - the six legs from a clean install;
  - any self-directed work selection (the autonomous scheduler does not
    ship);
  - timer-driven operation over time.

## Prerequisites

`install.sh prereq` checks, read-only and before it changes anything:
- Python 3.9 or newer, with the `venv`, `ensurepip` and `sqlite3` modules;
- the commands `install sed ln readlink chown chmod useradd groupadd usermod
  getent`;
- that the release tree is complete;
- that the install, config and unit directories can be created.

Also needed (not checked by `prereq`):

- Linux with systemd (units, timers and lingering use it).
- Network egress during the `venv` phase, for pip.
- Ollama on `localhost:11434` with the models `phi3:mini` and
  `nomic-embed-text`. `install/preflight.sh` checks that both are pulled.
  `nomic-embed-text` produces the memory embeddings; `phi3:mini` serves the
  local tier.
- Rootless Podman for the career/toolbox path. The tree includes
  `shared/toolbox/Containerfile`; build the image
  `localhost/treebeard-toolroom:latest` locally before enabling that path.

### Model tiers

- The tiers are defined in `TIERS` in `shared/lib/router.py`.
  - The local tier requests `phi3:mini` from Ollama.
  - The hosted tiers carry aliases (`free_v1`, `cheap_v1`, `frontier_v1`,
    `advanced_a_v2`, `advanced_b_v1`), and nothing in this edition maps an
    alias to a real model name.
  - To use a hosted tier, set its `model` in `TIERS`. There is no
    configuration file or variable for model names. This edit is untested.
- The free, cheap, advanced_a and advanced_b tiers take their endpoint from
  `TREEBEARD_<TIER>_API_BASE` and expect an OpenAI-compatible
  `POST <endpoint>/chat/completions` with `Authorization: Bearer <key>`.
- **Hosted keys.**
  - Each hosted tier reads its key from the environment variable named by
    `key_env` (for example `TREEBEARD_CHEAP_API_KEY`).
  - The systemd units load `/etc/treebeard/credentials/providers.env`. A
    process started by hand does not.
  - A tier whose `TREEBEARD_<TIER>_ENABLED` is unset is allowed. Only an
    explicit false value turns it off, so a key placed in `providers.env` is
    usable by default.
- **The local tier and governed turns.**
  - The router skips the local tier for prompts estimated over 4000 tokens,
    even when a tier is forced.
  - Every governed prompt starts with the constitution, about 8,000 tokens by
    the router's estimate, so **governed turns exceed the router's
    4,000-token local-tier limit and cannot use it**. A governed turn needs a
    hosted tier with a valid credential.
  - Short ungoverned calls can use the local tier.
- **The frontier tier** needs a private adapter module, loaded from
  `/opt/treebeard/private`, which this edition does not ship. If you add one,
  that directory and every file in it must be owned by root and writable by
  no other account. The router imports from it **without checking** (see
  Security notes).
- **Meeseeks pinning.**
  - Meeseeks calls are pinned: as shipped, the review ladder
    `MEESEEKS_TIERS` is `free,cheap,cheap`, set by the reflector unit and by
    the `shared/meeseeks/meeseeks` wrapper.
  - If the pinned tier refuses, the section is blocked.
  - The local tier is not proven for Meeseeks work: measured outputs were
    2.9k–6.3k tokens.

## Install

    sudo install/install.sh all
    # or phase by phase:
    sudo install/install.sh prereq users dirs code venv config initdb units linger preflight

Run it from the root of the release tree.

**Environment overrides** include:
- `TB_BASE` (default `/opt/treebeard`) and `TB_ETC` (default `/etc/treebeard`);
- `TB_UNIT_DIR`, `TB_AGENT_USERS`, `TB_BACKUP_USER`;
- `TB_TZ`, the timer time zone;
- `TB_ENABLE_TIMERS`.

Use the default `TB_BASE` and `TB_ETC`. Other values are accepted with a
warning but are only partly honoured, and relocation is untested.

**What the installer creates:** every directory and store it manages, valid
empty state files, the telemetry schema, and the systemd units.

**What it does not create:**
- `/etc/treebeard/credentials/providers.env`;
- the per-agent files `/etc/treebeard/credentials/<agent>.env` that the sun
  and moon units require;
- `/etc/treebeard/dispatch_ops.conf`;
- `/opt/treebeard/private`;
- the operator files listed below.

**Credentials.** The installer never generates credentials. It installs
`/etc/treebeard/credentials/providers.env.example`. Copy it to
`providers.env`, populate it, and make it mode 0600, owned by root; preflight
enforces this.

**`treebeard.env`.** `treebeard/ask.py` also requires
`/etc/treebeard/credentials/treebeard.env`. The installer does not create it
and preflight does not check it.
- If the file is missing, a governed turn exits (`ask.py: [Errno 2] No such
  file or directory: …`). If it cannot be read, the turn exits with `ask.py:
  cannot read … (run with sudo).`
- It holds `KEY=value` lines and may be empty.
- It must be readable by the account that runs `ask.py`, and unreadable to
  every other account.

### Timers are not enabled by default

The `units` phase renders the timer units but enables none. To enable them,
re-run `units` with `TB_ENABLE_TIMERS` set to a space-separated list of
timer units.

In one clean-room boot, five timers were enabled with no credentials file:
- the digest, backup, career and intake services refused to start, because
  they require the credentials files;
- the reflector ran on an empty board and exited 0.

Timer firing over time has not been tested.

### What a successful install looks like

`install.sh all` ends with preflight. **On a fresh host, preflight exits
non-zero. This is correct, not a failure of the install:** it reports what
the operator has not supplied yet.

Where nothing has been supplied, expect:
- three FAIL lines: the credentials file, Ollama not reachable, and Podman
  not installed. Each prints the command that fixes it.
- three WARN lines, for the absent operator files `resume.txt`,
  `career_profile.txt` and `persona_tokens.txt`;
- an `ok` line containing `containment: NOT PROVISIONED`.

The install itself has succeeded when no phase before preflight printed an
`[install] ERROR` line and the run reached preflight.

### Installation limitations

- **Personality.** On a fresh install the router cannot read the personality
  bounds files (a permission defect), so personality is reported as
  **inert** and tier selection falls back to round-robin.
- **Anvil intake.** It fails on a fresh install, because its candidate
  directories are not created.
- **Sun's dream pass and the memory governance audit.** Both fail on a fresh
  install, because the vector store's files are not group-writable for the
  agents that use them.
- **Fixes for these are designed but deferred** past 1.0 (see Known
  deficiencies).

## Policy files (shipped, installed, enforced)

The release ships `etc/treebeard/constitution.txt` and
`etc/treebeard/treebeard.charter.txt`. `install.sh config` installs both as
`root:root 644`, and preflight fails until they are present with that owner
and mode.

## What the operator supplies (private, never shipped)

- `/etc/treebeard/credentials/providers.env`: provider keys.
- The career inputs:
  - a patrol roster, which the patrol path refuses to load if it fails
    validation;
  - a career profile and exclusion lists;
  - a resume;
  - a persona-token list of strings removed from generated career packets.

## First run

1. Configure at least one hosted tier with a valid credential.
2. Make `/etc/treebeard/credentials/treebeard.env` present and readable by
   the account that runs `ask.py`.
3. Ask an agent something:

       cd /opt/treebeard
       TREEBEARD_AGENT=treebeard shared/venv/bin/python3 treebeard/ask.py "What is your mandate?"

The reply is preceded by a status line naming the agent, the tier and model,
the routing reason and the memory notes. This has not been exercised against
a real provider from a clean install.

## Verify

    cd /opt/treebeard
    sudo -u treebeard shared/venv/bin/python3 shared/lib/regression.py

(Run as root, the reflector suite also fails: its untrusted-store check sees root-owned test files.)

Measured on fresh installs of RC6:

| environment | result |
|---|---|
| container without systemd | `44 PASS, 0 FAIL, 0 ERROR` |
| systemd host, timers disabled (the default) | `42 PASS, 2 FAIL`: `sprint1` check AT-18 and `boxread`, which both assert enabled timers |
| systemd host, the five timers enabled | `44 PASS, 0 FAIL, 0 ERROR` |

The five-timer run does not exercise the digest, backup, career or intake
services; those refuse to start without credentials (see Timers).

Some checks print SKIP because their runtime state does not exist yet. These
are expected preconditions, not failures.

## Containment

The containment source ships in `shared/ctl`, together with
`install/provision-containment.sh` and `install/rollback-containment.sh`.
`install.sh` **never** provisions containment.

**Unprovisioned (the default):**
- A Meeseeks section stops before any model call
  (`execution unavailable: not provisioned`). No generated code is executed.
- Preflight shows `containment: NOT PROVISIONED`. This is informational,
  never a failure.

**Provisioning** is an explicit root step:

```
sudo bash install/provision-containment.sh --plan                    # read-only: the plan and its token
sudo bash install/provision-containment.sh --apply --confirm=<token> # applies exactly that plan
sudo bash install/provision-containment.sh --check                   # consistency check; checkpoints the receipts baseline
```

- **The token** is bound to the plan and to the host facts it records. If
  any of those change between `--plan` and `--apply`, the apply refuses and
  changes nothing.
- **What it grants, and nothing else:**
  - two system accounts, `tbctl` and `tbjail`: no login shell, locked
    password, own group only;
  - the root-owned containment modules and `/usr/local/sbin/tb-commission`;
  - the commissioning receipts file;
  - named ACL entries for those accounts on the paths they need;
  - a pinned `kernel.yama.ptrace_scope = 1`;
  - **one** passwordless sudo transition:

    ```
    treebeard ALL=(tbjail) NOPASSWD:NOSETENV: /usr/bin/python3 -IB /opt/treebeard/shared/ctl/jail_prep.py request
    ```
- **Proof of what was granted.** Provisioning snapshots the host before and
  after, and refuses to finish unless the difference equals its recorded
  steps. Keep the receipt: it is the argument to rollback.
- **Requirements.** Default `TB_BASE`; a service account with no other sudo
  right; `ptrace_scope` exactly 1; sudo-rs ≥ 0.2.13 or classic sudo ≥ 1.9.15;
  and the `acl` tools.
- **Tested on.** Ubuntu 24.04 (classic sudo 1.9.15p5); Ubuntu 26.04 (classic
  sudo 1.9.17p2); and Ubuntu 26.04 (sudo-rs 0.2.13). Anything else is untested.

**The claim: runtime-contained, not operator-authenticated.**
- Generated code runs as the jail account, with no other group, no
  capabilities and NoNewPrivs, under the system Python.
- In the acceptance probe, all 35 access attempts from the jail account were
  denied.
- **What it does not do:**
  - no namespace or network confinement;
  - no process-lifetime guarantee;
  - the board file stays writable by the service account;
  - project content after commissioning is not protected.

**Check and remove.**
- `--check` and preflight report PROVISIONED, NOT PROVISIONED or
  INCONSISTENT.
- `sudo bash install/rollback-containment.sh <receipt>` removes exactly what
  the receipt records, and refuses if the state has drifted.
- Rollback archives receipts and records; it never deletes them.
- Rollback exit 1 (`COMPLETED WITH DIFFERENCES`) can be expected, for example
  when project data is retained. Read the listed differences.

## Known deficiencies and open defects

These are in the shipped code. They are documented here and remain open in
this release, except where marked fixed in this tree.

**From the RC6 acceptance run (31 failures, tracked in a defect register):**
- **Fixed in this tree:** 9 of them, including the `ask.py` exit-status and
  stderr defects, the local-tier name and the reinstall `sms.py` event (the
  count is of tracked defects, not of the groups named).
- **Open**, the main items:
  - a Meeseeks section blocked by a rate-limited tier is not reported with
    the expected `FACTORY_EXHAUSTED` reason, and the reference-search stage
    did not run as specified;
  - an unreadable telemetry store is not named as a budget-check failure;
  - the reflector does not write `FAILED` for a flagged card, and on a
    failure leg it set the card to BLOCKED where the contract expects it
    unchanged;
  - malformed in-turn read blocks still trigger a continuation request;
  - the toolbox skill documents ship, but no shipped caller reads them;
  - one rate-limit retry test saw one retry on the cheap tier where its
    contract expected two.
- **The career intake job (`clark`) crashes when any of its three API keys
  (inbound e-mail, SerpAPI, Serper) is unset:** it raises `KeyError` and exits 1. A fix is written and tested outside this tree; it is not
  included.
- In one acceptance test, Clark's fit review dropped a seeded candidate.
  This has not been diagnosed.
- The résumé rule set is finite; it is extended as new commentary phrasings are found.
- **Fresh-install permission defects (designed, deferred past 1.0):**
  - the personality bounds are unreadable to the router;
  - the vector store is not group-writable;
  - the anvil candidate directories are missing;
  - the mission and lock writers cannot record their refusal receipts.

  The corrections are designed and reviewed. One of them did not pass
  its containment proof re-run. One question, which
  account runs the memory governance audit, awaits a ruling.
- **Router selection.** On a governed call where the local tier is in band,
  selection can pick another rung first. So the expected local
  `skip:context-capacity` row does not appear. Analysis is deferred.
- **Telemetry.** Mission-selection telemetry rows ignore the synthetic-run
  flag.
- **Preflight** exits non-zero on every fresh install by design (see
  Install). Earlier acceptance contracts expected exit 0.

**Also fixed in this tree (career-packet suite; gate-2 row I-26):** Employer-facing résumés are validated by a fixed rule set before delivery (rendered PDF and text); Clark's notes, fit comments and warnings go only to the operator's e-mail; if every tailored attempt is rejected, the operator's base résumé is sent to the operator only, labelled UNTAILORED.

**Long-standing:**
- Model names can only be changed by editing `router.py`.
- AT-18 and `boxread` fail while timers are disabled. AT-18 also assumes the
  default timer time zone.
- Rollback after a reboot can report one difference,
  `net.ipv4.tcp_available_ulp`, which this release does not treat as
  volatile.
- Non-default `TB_BASE` / `TB_ETC` are only partly honoured.
- A refused message or dispatch is audited as `policy-error`, including in
  the expected first-run state.

## Security notes

- **No identity proof.** Operator identity is not authenticated. Root-gated
  steps are recorded, not attributed to a verified person.
- **The jail does not confine the network.** Anything world-readable on the
  host stays readable to the jail account.
- **The frontier adapter path** `/opt/treebeard/private` is imported by the
  router, and as root by preflight when it is configured, without ownership
  or permission checks. Keep it root-owned and unwritable by any other
  account.
- **Receipts integrity is checked, not enforced.** Receipts appended after
  the last successful `--check` are not protected against truncation back to
  that checkpoint. Run `--check` after commissioning.
- **Fresh-install permissions.** Some stores are created with modes that are
  too tight for their intended users (see Known deficiencies). None of those
  defects widens access to the jail account.
- **Credentials.** The install creates no credentials. Keep every credentials
  file readable only by the accounts that need it.
- Hosted tiers are enabled unless explicitly disabled: a key present in
  `providers.env` is usable by default (see Model tiers).

## License and status

- **Status:** research prototype, version 1.0.1, public edition. It is
  published to show architecture, governance and evaluation practice, not
  as supported software.
- **License:** GPL-3.0-only. See LICENSE.
- All files in this tree, including the constitution and charter texts, are
  released under GPL-3.0-only with the permission of their copyright holders.
- Redwin's Laws / Constitution: Copyright © 2026 Redwin Tursor.
- Treebeard source code: Copyright © 2026 TC Ricks.
- Treebeard concept and design: Redwin Tursor and TC Ricks.
