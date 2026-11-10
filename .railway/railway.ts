import { defineRailway, github, project, service } from "railway/iac";

// The Railway project of todobot. Railway does not read this file on deploys: the workflow
// .github/workflows/railway-config.yml runs `railway config plan` on pull requests that change it and
// `railway config apply` after they merge. Variables stay in Railway (unmanaged): this repository is public.
export default defineRailway(() => {
  const todobot = service("todobot", {
    // A push to main deploys at once. It does not wait for the commit's checks: release-confirmation is one of them
    // and waits for this deploy, so waiting deadlocks (2026-11-10, db0c1e4: SKIPPED). Branch protection already
    // lets into main only pull requests whose required checks (verify, acceptance-gate) passed.
    source: github("gutnikov/todobot", { branch: "main", checkSuites: false }),
    build: {
      buildEnvironment: "V3",
      builder: "DOCKERFILE",
      dockerfilePath: "Dockerfile",
      // Only changes to the app deploy; agentify/, .github/ and this file do not.
      watchPatterns: ["/src/**", "/pyproject.toml", "/uv.lock", "/Dockerfile", "/.dockerignore"],
    },
    healthcheck: "/healthz",
    preDeploy: "python -m todobot migrate",
    // One region: the Hobby plan deploys to a single one (two failed the first deploy).
    replicas: { "ams": 1 },
  });

  return project("todobot", {
    variables: { managed: false },
    resources: [todobot],
  });
});
