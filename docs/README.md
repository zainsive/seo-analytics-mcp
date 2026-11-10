# Docs pages

The pages served at `https://<host>/docs` (team) and `/docs/admin` (operator).
They are plain HTML with no build step and no dependencies.

## The placeholder

`index.html` contains `__ENDPOINT_PATH__` where the real endpoint path belongs.
It is **not** committed with the real value: the endpoint path is access
control, this repository is public, and anyone reading it would have the URL.

Substitute on deploy:

```bash
sed "s/__ENDPOINT_PATH__/$SEO_MCP_ENDPOINT_PATH/g" docs/index.html \
  | sudo tee /var/www/mcp-docs/index.html >/dev/null
```

Access tokens are never substituted in, placeholder or otherwise. Each person
has their own, so a shared page cannot carry one — the page says to expect it
separately, and `seo-analytics-mcp token mint <name>` issues it.

## Serving

Both pages sit behind HTTP basic auth in nginx, which is what returns `401` on
`/docs`. That is separate from the bearer token on the MCP endpoint itself: one
protects reading the instructions, the other protects the data.
