# Docs pages

The pages served at `https://<host>/docs` (team) and `/docs/admin` (operator).
Plain HTML, no build step, no dependencies.

## Deploying

```bash
scp docs/index.html stg-vps:/tmp/d.html
ssh stg-vps 'sudo install -m 644 /tmp/d.html /var/www/mcp-docs/index.html'
```

No substitution step. These used to carry `__ENDPOINT_PATH__` in place of a secret
endpoint path, because that path was the access control and this repository is public.
It is not the access control any more — every request needs a credential — so the
endpoint is plain `/mcp` and the committed pages carry the real URL.

Access tokens are still never written into these pages. Each person has their own, and
since Google sign-in went live most people need no token at all.

## Serving

Both pages sit behind HTTP basic auth in nginx, which is what returns `401` on `/docs`.
That is separate from the credential on the MCP endpoint itself: one protects reading
the instructions, the other protects the data.
