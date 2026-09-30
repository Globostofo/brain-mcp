# heracles-brain

Remote MCP server exposing the brain vault (`Globostofo/brain`) to claude.ai (web, desktop, mobile) as a custom connector.

- Works on its own clone of the repository: pulls before reads (at most every 30 s) and before writes, commits `sync: heracles <date>` and pushes after each write.
- Tools: `list`, `search`, `read_note`, `write_note`, `edit_note`. No deletion or move. Hidden directories (`.git/`, `.sync/`, …) are neither readable nor writable, and only `.md` files can be written.
- Auth: the server is its own OAuth authorization server for claude.ai (dynamic client registration, same domain as the MCP endpoint) and delegates the actual login to the SSO (`auth.globostofo.net`) through a pre-registered OIDC client. Access is restricted to Romain's account by a policy binding in the SSO.
- Publicly exposed on `brain.globostofo.net`: an exception to the VPN-only rule, since claude.ai's servers are the ones calling the connector.

## Prerequisites

- Docker + Docker Compose
- `heracles` Docker network (created by heracles-proxy)
- heracles-proxy and heracles-auth running

## Setup

### 1. Deploy key

**[prod — RPi]** Clone the repo and create an SSH key with write access to the notes repository:

```bash
git clone git@github.com:Globostofo/heracles-brain.git ~/heracles-brain
cd ~/heracles-brain

ssh-keygen -t ed25519 -f deploy_key -N "" -C heracles-brain
# The container runs as uid 1000
sudo chown 1000:1000 deploy_key && chmod 600 deploy_key
cat deploy_key.pub
```

**[dev]** Register the public key on the notes repository, with write access:

```bash
gh repo deploy-key add deploy_key.pub --repo Globostofo/brain --allow-write --title heracles-brain
```

(or paste it in GitHub → `Globostofo/brain` → Settings → Deploy keys → Add, tick "Allow write access").

### 2. OIDC provider in the SSO

**[dev — browser, via VPN]** In the admin panel (`http://192.168.1.123:9000/if/admin/`; it is blocked on `auth.globostofo.net`, see the heracles-auth README):

1. Applications → Providers → Create → **OAuth2/OpenID Provider**
   - Name: `brain`
   - Authorization flow: explicit consent
   - Client type: **Confidential**
   - Redirect URIs: `strict` — `https://brain.globostofo.net/auth/callback`
   - Signing key: select a certificate (tokens must be RS256 JWTs verifiable with the JWKS)
   - Scopes: `openid`, `email`, `profile`
   - Note the **Client ID** and **Client Secret**
2. Applications → Applications → Create
   - Name: `brain`, slug: **`brain`** (it is part of `OIDC_CONFIG_URL`), provider: `brain`
3. Open the `brain` application → Policy / Group / User Bindings → Bind existing user → **your own user only**, order 0.
   Without this binding, any player account could log in and read or write the brain.

### 3. Environment

**[prod — RPi]**

```bash
cp .env.example .env

# JWT_SIGNING_KEY
openssl rand -base64 36

# Fill in OIDC_CLIENT_ID, OIDC_CLIENT_SECRET and JWT_SIGNING_KEY
nano .env
```

## Deploy

**[prod — RPi]**

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
docker compose logs -f brain
```

The first start clones the notes repository into the `vault` volume. OAuth state (registered clients, tokens) lives in the `oauth` volume, encrypted with `JWT_SIGNING_KEY`.

Update:

```bash
git pull
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

## Check

**[dev]**

```bash
# Both must point to https://brain.globostofo.net (not to the SSO domain)
curl -s https://brain.globostofo.net/.well-known/oauth-protected-resource/mcp
curl -s https://brain.globostofo.net/.well-known/oauth-authorization-server

# Must return 401
curl -s -o /dev/null -w "%{http_code}\n" -X POST https://brain.globostofo.net/mcp
```

## Add the connector in claude.ai

**[claude.ai — browser]** Settings → Connectors → Add custom connector:

- Name: `Brain`
- URL: `https://brain.globostofo.net/mcp`
- Leave the advanced OAuth settings empty (the server supports dynamic client registration)

Then Connect, log in with your SSO account and approve. The connector is then available on web, desktop and mobile.

## Development

**[dev]**

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

Tests use a local bare repository as the remote: no network access, no GitHub push.

Run locally without auth, on a throwaway clone (refused when `BASE_URL` is https):

```bash
git clone -q --bare ~/brain /tmp/brain-origin.git
BRAIN_NO_AUTH=1 BASE_URL=http://localhost:8000 BRAIN_DIR=/tmp/brain-test \
  BRAIN_REPO=/tmp/brain-origin.git .venv/bin/heracles-brain

npx @modelcontextprotocol/inspector   # connect to http://localhost:8000/mcp
```

Writes are pushed to `/tmp/brain-origin.git`, never to GitHub.
