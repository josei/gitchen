# Working on gitchen

Notes for anyone (human or agent) changing the framework. This is not the
prompt of any gitchen agent; those live in each instance repo's `agents/`.
Record design decisions here, with the reason, so they are not re-litigated.

## Four things that must stay separate

| What | Is | Holds | Must not hold |
|---|---|---|---|
| **gitchen** (this repo) | the framework: daemon, MCP server, kitchen view, landing page | mechanics: dispatch, issue states, permissions, artifacts, the `NEXT:` directive | any project's product knowledge, issue numbers, role policy, machine paths |
| **gitchen's library** (`library/`) | optional, product-agnostic starting prompts for generic roles | what a role is *for*, in terms that fit any repo | product names, instance conventions, response mechanics (see below) |
| **an instance repo** | one project's orchestration: `issues/`, `agents/`, `project.yml`, `registry.yml`, knowledge, schedules | that project's roles, their policy, issues, priorities, allowed tools | a copy of the framework, or the framework's internals |
| **the project repo** | the product's code, which agents change on `issue/<id>` branches | the product | anything about agents, issues or gitchen |

Rules that follow:
- Framework code, tests, docs, the landing page and library prompts use
  neutral examples (`my-project-agents`, `builder`), never a real instance,
  product, issue number or commit. Real names appear in an instance's own
  files only.
- Flow is one way: an instance points at gitchen (`registry.yml` pins
  library prompts by commit; the daemon runs from this checkout); gitchen
  never reads an instance's files except through its configured
  `--project-root`. The project repo knows nothing of either.
- A role prompt in an instance may say *which* verb fits its policy (the pm
  merges, cancels or blocks); the verb mechanics and wording of the
  directive line come from the daemon.
- When fixing a bug seen in an instance, write the test and the comment in
  generic terms ("a reviewer's screenshot"), not "issue #172".

## Decisions

### Response rules belong to the framework, not to role prompts

**Decision.** Everything about *how an agent's response is posted* is
injected by the daemon in `_build_prompt` (`mcp/dispatch.py`), identically
for every role: that the final text becomes a markdown comment, how to embed
an image or link a file, that the daemon copies and rewrites linked
artifacts, where the artifacts folder is, and the closing `NEXT:` line. Role
prompts (`library/agents/*.md` and each instance's `agents/*.md`) say only
what the role is *for*: what to investigate, review or build, and *which*
things to show (e.g. "screenshot every feature you reviewed"). They never
say *how* to show it.

**Why.**
- A role prompt that carries syntax is wrong the day the convention changes,
  and the pinned copies in instance repos (`registry.yml`) go stale and
  disagree with the daemon.
- Every role needs the same rules. Copying them into each prompt, including
  generic library ones, means a new or user-defined role silently lacks them.
- It already failed once: the reviewer prompt showed the image syntax inside
  backticks, the model copied the code form, and a reviewer's screenshot rendered
  as text instead of a picture. The framework prompt now gives the example
  unwrapped, says plainly that a path in backticks shows nothing, and the
  daemon embeds a real untracked image named in backticks anyway.

**How to apply.**
- Changing how comments, artifacts or directives work: change `_build_prompt`
  / `_relocate_artifacts` and their tests, never a library prompt.
- Writing or editing a role prompt: do not mention markdown syntax, the
  artifacts folder, link rewriting or the `NEXT:` format. Ask for the
  content ("a screenshot per feature, with a caption naming it"); the
  framework supplies the mechanics.
- A role-prompt edit that does belong in the library: commit it, unpin that
  line in the instance `registry.yml`, run `gitchen sync`.

### The daemon never comments on an issue

**Decision.** Comments are what roles report. Anything the framework does or
notices (a circuit breaker trip, a crash, a directive that never came, a
refused close, a stacked issue unblocking) is an event in the assignment
history. A refused close is sent back to the running agent, in its own
session, so it can act on the reason.

**Why.** A daemon comment looks like a role's report and confuses whoever
reads the issue, and it made the next role's prompt depend on text the
framework invented. Events are structured, and the view and the next prompt
both read them.

### Budget is per instance, not per account

**Decision.** The budget is set in each instance's `project.yml` and counted
from its own recorded costs. It stops new turns at the limit and never
anticipates an issue's cost. A person resets it.

**Why.** A limit on the whole account leaves no headroom for interactive work.
Stopping at the limit, rather than estimating, keeps the mechanism simple and
honest: a running turn always finishes.

### Costs are recorded with the final comment, and include the model

**Decision.** A turn's cost is written in the same commit as its report, once
per model, with the time it took.

**Why.** Each report then carries what it cost, and a retro can total them.
Writing cost separately would need its own failure handling.

### No `parent` field

**Decision.** An issue that splits another refers to it in its body. Nothing
records the link.

**Why.** A link field would be the only structure in the issue model that
means something the framework computes from. Agents can derive what they need
from the text, so the model stays small.

### Blocked waits on other issues; no conditions, no escalate verb

**Decision.** `NEXT: blocked_by #12 #13` waits until those issues close. There
is no condition language and no `escalate` verb. A role that is stuck reworks
its criteria, splits the work, or uses a stronger role, and blocks only what
it cannot resolve.

**Why.** Condition languages grow without end. Escalation is just reassignment
under another name, and a prompt can express it.

### Labels mean nothing to the framework; folders scope editing

**Decision.** The framework never reads or adds a label. Who may edit which
issues is decided by folder, not label, so a role cannot relabel its way into
scope.

**Why.** Labels are an instance convention. Editing scope is a permission, and
a permission must not be changeable by the thing it restricts.

### Merges rebase in the daemon; tests stay with the reviewer

**Decision.** When a base has moved, the daemon rebases the branch and sends it
back to the reviewer to verify again. The daemon never runs tests or knows a
test command.

**Why.** A test command in the daemon makes it a CI system. The reviewer is
already the verifier.

### A rebased branch keeps its base until it is checked again

**Decision.** When the daemon rebases a branch during a turn, it reserves that
base for the issue until the turn ends. Any other merge into that base waits:
a `NEXT: merge` waits silently and the agent never hears of it, while a tool
call is refused with the reason. The first rebase in a turn uses no
`directive_retries`. The reservation lives only in the daemon's memory.

**Why.** Without it, a base that moves faster than a check runs keeps sending
the branch back, and the retries run out on a race, not on the work. Landing
optimistically and catching breakage later needs CI, which the framework does
not have, so the tree that lands must be the tree that was checked. Agents only
ask to merge, so waiting is the daemon's business, not theirs. Holding it in
memory means a restart clears it, so nothing can deadlock. A merge from another
process (a person's own session, a commit by hand) does not see it and lands
like any other commit. The rebased issue then needs one more check, and that
check counts against its retries.

### No approvals system for now

**Decision.** The human gate is the budget and its reset, not per-action
approval. The read-only view stays read-only.

**Why.** An approval flow needs writes from the front end and a record of
decisions. Neither is designed, and a budget already stops work when it should.

### Mechanical work that needs tools is a program role, not an LLM

**Decision.** A role whose work is fixed steps (poll a feed, file what it finds)
is a script named in its prompt's `run:` frontmatter, with `gitchen call` for the
tools. It runs with the same permissions an agent's calls have, and it records no
cost. A schedule may run one with no issue at all.

**Why.** A model turn for a step with no judgment in it spends tokens to do what a
script does for nothing, and its output is harder to trust. A script keeps the
authorization in one place: the daemon's control socket.

### Per-role network and workspace are what a role needs, not how a machine runs

**Decision.** A project may name a role's `network` (the hosts it reaches) and
`workspace: none` (no repository at all). Everything else about how a turn runs is
machine config.

**Why.** A collector that reads one feed should not reach every host the machine
allows, nor be given a checkout it has no use for. The machine still decides the
sandbox, so a project can't widen it beyond what the machine permits.


### The prompt is ordered by how widely each part is shared

**Decision.** `_build_prompt` emits shared context, workspace rules, posting
rules and the directive rule first; the role prompt, edit scope and assignable
roles next; then the issue, its branch/base and artifacts folder, and its
history. Only the role-specific directive values are last. The issue section
carries the id, branch and artifacts path, so the text before it is identical
for every issue a role runs on. The web view's role panel omits the static
parts (shared context, workspace, posting rules, directive rule) and shows the
directive values as a compact footer.

**Why.** Prompt caches are prefix-based. With the role prompt first, two roles
share no cached bytes at all; with the widely-shared text first, every role's
prompt starts with the same block, and the next issue for the same role reuses
even more. The directive rule is common to every role, so it belongs in the
prefix; only the values (`NEXT: built`, `NEXT: stuck`) vary, and they stay last
so the turn sees its actual options when it finishes.

### A schedule never stacks its own unfinished issue

**Decision.** A schedule skips a cron match while the issue it filed last is
still `open` or `blocked`.

**Why.** A recurring round, like a strategist that waits on its own bets, can
outlast its period. Filing another round on top of it would run two plans at
once and spend twice. The next match after it closes files the next round.

