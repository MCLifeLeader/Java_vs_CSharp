# Repository guidance

Read `docs/WORKLOG.md` before resuming work. Update it at meaningful checkpoints
and before ending a session with changes: decisions, completed scope, verification
commands/results, unresolved work, and the next concrete action.

The user requested the full benchmark suite, using .NET 10 LTS and Java 25 LTS,
and authorized sub-agents. The original C# 15/Java 27 track is deferred.
Use https://github.com/ICS-Eng/Edu-Developer-Toolbox/wiki as guidance.

For every repository, include current test totals when generating or updating
its wiki. Break totals down by framework and test area, and record the date,
exact source commit, counting method, and verifiable inventory or execution
evidence.

Distinguish authored definitions, discovered cases, and executed results. Keep
parameter expansion, repeated browser or role executions, snapshots, and
standalone contract scripts separate. Report passing, failing, and skipped
results when evidence exists; otherwise state that tests were not run for the
wiki update. Updating a wiki alone does not require running the full suite.

Report code-coverage percentages only from a measured coverage report. Label
coverage as unavailable when none exists; test counts are not coverage
percentages and do not establish assertion quality.
