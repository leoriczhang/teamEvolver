# True Replay

For every Test Dataset Case, True Replay opens isolated Baseline/Candidate sessions with identical
query, materials, Context, model, tools and limits. Treatment is the only intended difference.

1. Open the sessions and send only the dataset query in the first interaction.
2. Collect real Agent replies, traces, artifacts and observed metrics.
3. An independent judge verifies each Checklist item with concrete evidence.
4. If incomplete, select the next permitted requirements deterministically, then use a separate user simulator to render natural feedback.
5. Record feedback as a user message and continue; close sessions on completion or limits.

Adapters never see Checklists, judge state or hidden requirements.
Feedback expresses only current goals/gaps, with no IDs, scoring or A/B terminology and no invented praise.
Missing metrics are unavailable; judging fails closed.
Efficiency uses interactions, monotonic elapsed time and real tool/token observations.

Accept when only Candidate completes, reject when only Baseline completes, and return inconclusive
when both fail or judging is unavailable. Compare efficiency only after both complete.
Audit records retain evidence, conversation, disclosure and adapter source revision.
See [Replay API](../api/05-replay-branch.md) for contracts, permissions and binding.
