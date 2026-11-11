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
  ci: "GitHub Actions of gutnikov/todobot via gh (checked 2026-11-11: gh run list). Workflows verify, acceptance-gate, spec-check (required on main), release-confirmation, railway-config"
  tracker: "GitHub issues of gutnikov/todobot via gh, read only (checked 2026-11-11: gh issue list, 0 issues)"
  deploy: "Railway via the railway CLI 5.71.0 (checked 2026-11-11: railway status): project todobot, service todobot, environment production"
  logs: "Railway service logs via railway logs --json (checked 2026-11-11: JSON lines of version 9d74f7c)"
  browser: "agent-browser 0.20.14 CLI and the Chrome DevTools MCP (both checked)"
  errors: "PostHog error tracking via the posthog MCP server (checked 2026-11-11: projects-get, project Default project, 277751). todobot sends nothing to it yet: the project's events come from another app"
  metrics: "PostHog product analytics, the same MCP server and project; nothing from todobot yet"
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

Since 2026-11-10 the skeleton (SK) runs in production as @sortmyshit_bot: it keeps every message in the sender's
inbox and does nothing else with it yet. The user has accounts on Railway (hosting), Neon (Postgres) and PostHog
(analytics and error tracking) for it.

## Notes

- Neon, the production database (project todobot, `twilight-firefly-94202512`, aws-eu-central-1, Postgres 16), is
  reached with neonctl: `npx -y neonctl@latest <command> --org-id org-autumn-math-23942753` (checked 2026-11-11:
  projects list). The org is on Neon's paid Launch plan: anything that adds to its cost is H2.
- PostHog answers through its MCP server since 2026-11-11 (the user signed in; in this machine's Claude Code
  config at local scope, as `.mcp.json`'s entry waited for an approval the session never asked). Its one project,
  Default project, also takes another app's events: until todobot sends its own (a product task, perhaps with a
  project of its own), PostHog says nothing about todobot, and no loop reads it.
- Neon's MCP server (`neon` in `.mcp.json`) is not approved; neonctl above covers what the agent needs.
