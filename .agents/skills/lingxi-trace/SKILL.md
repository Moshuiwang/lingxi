---
name: lingxi-trace
description: Plan or orchestrate Lingxi Execution Traces with strict planner separation, provider-neutral Codex or Claude handoff, parallel implementer subagents, one independent reviewer, and mandatory batch-to-batch orchestrator succession.
---

# Lingxi Trace execution adapter

This skill is a thin execution adapter. It does not duplicate the method or a concrete contract:

- Planners read GitHub Issue [#147](https://github.com/Moshuiwang/lingxi/issues/147) and the minimum product, technical, Issue, and live facts needed to create or amend a Trace.
- Orchestrators read only the approved `docs/traces/<issue>-<name>/` three-file contract and its bound sources. Missing contract fields go back to the planner; do not fill them from #147 or chat history.
- Claude Code reaches this same file through `AGENTS.md`; do not create a second Claude-only rule body.

## Choose exactly one role

- **Planner:** establish reachability, gates, stable Step IDs, leases, evidence, authorization, model mapping, and the execution adapter. Merge the planning PR, then stop. Never implement in the planning task.
- **Orchestrator:** start in a fresh top-level task or named session, record takeover, route only Ready Steps, own the single shared write / merge lease, freeze candidates, coordinate the single reviewer, and close or hand off the batch. Do not implement product changes yourself.
- **Implementer:** work only inside the dispatched Step and dedicated worktree, implement and self-test end to end, commit before reporting, and do not spawn subagents.
- **Reviewer:** review the fixed candidate independently, do not edit it, and leave one explicit blocking / non-blocking conclusion. The same reviewer performs targeted re-review after fixes.

## Apply the Trace adapter

- `codex_new_task`: create a fresh top-level Codex task for each orchestrator, using the model and reasoning effort recorded in the Trace.
- `claude_tmux_named_session`: create a recoverable named interactive tmux session using the Trace mapping.
- Do not switch provider, adapter, model, or reviewer count automatically. Such a switch is valid only at a batch boundary with explicit product-owner authorization recorded in the Trace.

## Execute a batch

1. Read the three files and latest takeover / closeout comment; verify baseline, Ready Steps, candidate rule, authorization, and leases.
2. Dispatch as many independent Steps as current concurrency and leases safely allow. Every mutating parallel agent uses its own worktree; one Story has one Owner.
3. Integrate in the contract order, freeze one candidate, and send that exact object to the Trace's one independent reviewer. CI and implementer self-checks are evidence, not extra reviewers.
4. Put all accepted findings into one defect ledger and one repair package. Candidate changes invalidate the old review; use the same reviewer for targeted re-review.
5. Record the batch result and reconcile Issue / documentation facts. Fact-reconciliation agents do not create a second candidate-review conclusion.

If work remains, close the batch in this order: closeout comment, self-contained handoff comment, create successor top-level task or session, successor takeover record, atomic transfer of the write lease, then old orchestrator exit. Never continue by clearing the old context and calling it a new orchestrator.

Use the repository's approved GitHub and Git identity wrappers. Public comments contain only minimum necessary operational facts and never credentials, private host identifiers, or secrets.
