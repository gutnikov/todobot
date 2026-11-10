---
# What only the user decides about agentify in this project. The first /agentify proposes these from what it found
# and the user's answers; after that they change only with the user's approval (H7). The scripts read the fields
# here; every task reads the whole file.
#
# What the project ships, one or more of: web, api, mobile, library, data, llm, infra.
kind: [api, llm]
# How far agentify goes on its own: ask, branch, merge or deploy (the harness part's README, "Preferences").
autonomy: deploy
# Where a change counts as done, and the super-loops end: local, ci, staging or production.
done_at: production
# What the agent can use from its own session, each checked with one real call or named by the user.
# Keys: ci, logs, errors, metrics, traces, browser, deploy, tracker. Value: what and how, e.g. "GitLab CI via glab".
tools:
  ci: "GitHub Actions via gh (logged in as gutnikov; checked: gh repo list). No todobot repo yet"
  tracker: "GitHub issues via gh (same login). No todobot repo yet"
  deploy: "Railway via the railway CLI (logged in; checked: railway list). No todobot project yet"
  logs: "Railway service logs via railway logs (same CLI). Nothing deployed yet"
  browser: "agent-browser 0.20.14 CLI and the Chrome DevTools MCP (both checked)"
# The spend limit in USD for evals and inferential loops; null means no limit.
budget_usd: null
# The spec, once it is set up (the spec part): who answers questions about requirements and approves the spec,
# such as "the user", a name, or "the ticket's reporter"; a person always approves (H5).
spec_approver: "the user"
# Who turns the spec into tests: "agent", a test writer with a fresh context, or a person, such as "QA".
test_writer: agent
# What agentify never invents, whatever the sources hint at: each is a question to the spec approver.
never_invent:
  - prices, credits, quotas and limits
  - who may do what (roles and permissions)
  - legal, compliance and consent texts
  - the wording of errors and of anything about money
  - deadlines, timeouts and retry counts
---

# todobot

In the user's words:

> A telegram bot that takes any input (text, audio, img) and organises it into a managable task system: google
> calendar, daily task lists, respects priorities. It also elaborate on your vague tasks in the background, so when
> you back it can ask you folloup questions during the time your are available

Nothing is built yet (2026-11-10). The user has accounts on Railway (hosting), Neon (Postgres) and PostHog
(analytics and error tracking) for it.

## Notes

- PostHog and Neon are reached through their MCP servers, `posthog` and `neon` in `.mcp.json`. They wait for the
  user's approval and sign-in (`/mcp`). Once each answers one real call, PostHog goes into `tools` as `errors` (and
  `metrics`), and Neon is noted here as the database the agent can reach.
