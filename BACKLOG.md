# Backlog

Non-blocking follow-ups and open questions surfaced during development. Roadmap
phases live in `development.md`; this is the loose-ends list.

## Open questions

- **Google conversion-action scoping.** Does AdLoops support scoping the audit
  to a *specific* Google Ads conversion action ID, or does it read campaign-level
  total conversions? (Came up when OpenClaw wired a conversion action,
  `2026-06-03`.) Needs a code check of the Google read path in `mcp_clients.py`
  before we document the behavior either way.

## Known limitations (verified)

- **Non-USD budget changes default to USD.** The proposer never sets
  `after.currency`, and the executor forwards currency only if set
  (`mutations.py`, `executors/linkedin.py`), so budget-change mutations on a
  non-USD account can default to USD. Relevant for EUR accounts. Verify/eyeball
  the first real budget mutation; proper fix is to thread the account currency
  through the proposer.
- **`LINKEDIN_ACCESS_TOKEN` staleness.** The read path uses a static env token
  that expires (~60 days) while the MCP write path auto-refreshes — reads will
  start 401-ing in ~2 months. Re-export from the token store when that happens;
  proper fix is to have the read client pull from the refreshing token store.
  (Also noted in `linkedin-golive.md`.)

## Performance / nice-to-have

- **Batch LinkedIn/Meta AUTO dispatch.** `run.py` dispatches non-Google AUTOs
  per-call (a Node MCP spawn per LinkedIn mutation). If real runs produce
  multiple LinkedIn AUTOs, mirror `_dispatch_google_batch` so one
  `LinkedInExecutor` session serves the batch. (From `RESUME.md`.)
