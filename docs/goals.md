# Goals backend

Finspace Goals are an independent planning domain. A Goal stores target metadata and an explicit
lifecycle; its progress is projected from immutable `GoalContribution` events. Goals do not read or
write accounts, transactions, Budget, Month Close, Google sync, recurring rules, or n8n.

Normal contributions are positive and accepted only while a Goal is active. Corrections are signed,
append-only events linked to one original contribution. They may repair history in any non-deleted
lifecycle state, but neither an original contribution nor total Goal progress may become negative.

All Goal mutations require `X-Idempotency-Key`. Successful command responses are stored exactly and
replayed verbatim; clients should fetch the Goal again after a contribution command to obtain the
latest live projection. Metadata and lifecycle changes use optimistic Goal versions.

Migration `0010_goals` creates only `goals`, `goal_contributions`, and `goal_command_results`.
