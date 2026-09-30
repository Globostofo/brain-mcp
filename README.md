# heracles-brain

Remote MCP server exposing the brain vault (`Globostofo/brain`) to claude.ai (web, desktop, mobile) as a custom connector.

- Works on its own clone of the repository: pulls before reads (at most every 30 s) and before writes, commits `sync: heracles <date>` and pushes after each write.
- Tools: `list`, `search`, `read_note`, `write_note`, `edit_note`. No deletion or move. Hidden directories (`.git/`, `.sync/`, …) are neither readable nor writable, and only `.md` files can be written.
- Auth: the server is its own OAuth authorization server for claude.ai (dynamic client registration, same domain as the MCP endpoint) and delegates the actual login to the SSO (`auth.globostofo.net`) through a pre-registered OIDC client. Access is isolated from the games: dedicated SSO account, login flow and signing key, plus a username check in the server.
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

### 2. Isolated access in the SSO

Goal: players cannot reach the brain, and the brain account cannot reach the games. The SSO has a single user directory shared by all apps, so the isolation relies on a dedicated account, flow, signing key and bindings.

**[dev — browser, via VPN]** In the admin panel (`http://192.168.1.123:9000/if/admin/`; it is blocked on `auth.globostofo.net`, see the heracles-auth README):

1. **Group and account**
   - Directory → Groups → Create: `brain`.
   - Directory → Users → Create: username **`brain`** (must match `ALLOWED_USERNAME`), type **Internal**, not superuser. Add it to the `brain` group, then set a strong password (user → Set password).
2. **Dedicated login flow**: Flows and Stages → Flows → Create
   - Name/slug: `brain-authentication`, designation **Authentication**, behavior setting Authentication: **No requirement** (any other value denies the flow when opened directly), no policy bound to the flow itself.
   - Stage bindings, in order:
     - 10 — new Identification stage `brain-identification`: user fields **Username** only, **no sources** (no Google/Discord buttons), no enrollment/recovery flow.
     - 20 — new Password stage `brain-password`.
     - 30 — new Authenticator Validation stage `brain-mfa`: device classes **TOTP only**, "Not configured action": **Force the user to configure an authenticator**, configuration stage: `brain-totp-setup` (a TOTP Authenticator Setup stage created beforehand in Flows and Stages → Stages, 6 digits, not bound to any flow), last validation threshold `seconds=0`.
     - 40 — new User Login stage `brain-login`: session duration `seconds=0`, stay signed in offset `seconds=0` (a dedicated stage, so that tuning the default one for players never affects the brain).
3. **Signing key**: System → Certificates → Generate: common name `brain-signing`, validity `3650` days, private key algorithm **RSA** (the server verifies RS256 by default; an ECDSA key would fail). Tokens of the brain are not signed with the games' key.
4. **Provider**: Applications → Providers → Create → OAuth2/OpenID Provider
   - Name: `brain`
   - Authentication flow: **`brain-authentication`**
   - Authorization flow: `default-provider-authorization-explicit-consent`
   - Client type: **Confidential**
   - Redirect URIs: Strict, `https://brain.globostofo.net/auth/callback`
   - Signing key: **`brain-signing`**; Encryption key: empty (encrypted tokens could not be verified by the server)
   - Scopes (Advanced protocol settings): `openid`, `email`, `profile` **and `offline_access`** (without it the SSO issues no refresh token, and claude.ai is logged out when the access token expires, after 5 minutes by default)
   - Refresh token validity (optional): e.g. `days=90`, otherwise claude.ai asks to log in again after 30 days
   - Note the Client ID and Client Secret.
5. **Application**: Applications → Applications → Create, name `brain`, slug **`brain`** (part of `OIDC_CONFIG_URL`), provider `brain`. Then Policy / Group / User Bindings → Bind existing → Group → **`brain`**.
   An application without any binding is open to every SSO user: this binding is what keeps players out. The server also checks `ALLOWED_USERNAME` on every call, in case the binding is ever removed.
6. *(Optional, skipped in the current setup)* **Keep the brain account out of the games** — without it, the `brain` account could log in to a game like any player, but claude.ai never holds a token valid for the games: Customization → Policies → Create → Expression Policy `deny-brain-group`:
   ```python
   return not ak_is_group_member(request.user, name="brain")
   ```
   Bind it to every game application (e.g. Fisharia), then check that a player can still log in to the game.
7. **Enroll the second factor right away**: open `https://auth.globostofo.net/if/flow/brain-authentication/` in a private window, log in as `brain` and scan the TOTP QR code. Until this is done, anyone with the password could enroll their own device.

claude.ai never receives SSO tokens: the server keeps them and gives claude.ai its own tokens, only valid for this server.

### 3. Environment

**[prod — RPi]**

```bash
cp .env.example .env

# JWT_SIGNING_KEY
openssl rand -base64 36

# Fill in OIDC_CLIENT_ID, OIDC_CLIENT_SECRET and JWT_SIGNING_KEY (ALLOWED_USERNAME=brain is already set)
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

Then Connect, log in with the `brain` account (password + second factor) and approve. If the browser already has an SSO session with another account (e.g. a player account), the SSO reuses it and denies access: log out of `auth.globostofo.net` first, or connect from a private window. The connector is then available on web, desktop and mobile.

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
