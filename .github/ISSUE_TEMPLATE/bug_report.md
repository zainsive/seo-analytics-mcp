---
name: Something is not working
about: Setup problems included - most issues are setup, not bugs
labels: [needs-triage]
---

**Paste the output of `seo-mcp doctor --json`.**

This is the single most useful thing you can include. It contains no credentials -
only paths, versions, which checks passed, and which APIs answered.

```
paste here
```

**What did you ask, and what came back?**

**Which MCP client?** (Claude Desktop, Claude Code, other - and its version)

**Before you file:** if the server worked and then stopped after about a week, your
OAuth app is still in "Testing" publishing status. Google expires those refresh tokens
after 7 days. Press **Publish app** on the OAuth consent screen, then run
`seo-mcp auth --reauth`. See the README section "The seven-day problem".
