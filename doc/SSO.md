# SSO / OpenID Connect Configuration

Arcology supports OAuth 2.0 / OpenID Connect (OIDC) single sign-on via any
compliant identity provider — Keycloak, Okta, Azure AD, or others.  The feature
is disabled by default; existing local-auth deployments are unaffected until you
opt in.

---

## Table of Contents

- [Quick Start](#quick-start)
- [Configuration Reference](#configuration-reference)
- [Role Mapping](#role-mapping)
- [Account Linking](#account-linking)
- [Provider Setup Examples](#provider-setup-examples)
  - [Keycloak](#keycloak)
  - [Azure AD / Entra ID](#azure-ad--entra-id)
  - [Okta](#okta)
- [Session Management](#session-management)
- [Logout Behaviour](#logout-behaviour)
- [Migrating Existing Users to SSO](#migrating-existing-users-to-sso)
- [Troubleshooting](#troubleshooting)

---

## Quick Start

1. Create a confidential OAuth 2.0 client in your identity provider with:
   - **Redirect URI**: `https://arcology.example.com/auth/sso/callback`
   - **Standard flow** (authorization code grant) enabled
   - **Client authentication** enabled (confidential client)

2. Add to `myapp.cfg` (or as environment variables in `.env`):

```ini
OIDC_ENABLED        = True
OIDC_PROVIDER_NAME  = "Keycloak"
OIDC_DISCOVERY_URL  = "https://keycloak.example.com/realms/myrealm/.well-known/openid-configuration"
OIDC_CLIENT_ID      = "arcology"
OIDC_CLIENT_SECRET  = "<paste secret here>"
```

3. Create roles in your provider and assign them to users (see [Role Mapping](#role-mapping)).

4. Apply the database migration and restart:

```bash
flask db upgrade
```

---

## Configuration Reference

All settings can be placed in `myapp.cfg` (Python booleans/strings) or passed
as environment variables (strings; `True`/`true`/`1` are all accepted for booleans).

| Setting | Default | Description |
|---------|---------|-------------|
| `OIDC_ENABLED` | `False` | Master toggle. Set `True` to enable SSO. |
| `LOCAL_LOGIN_ENABLED` | `True` | Show the local username/password form alongside SSO. Set `False` to enforce SSO-only login. |
| `OIDC_PROVIDER_NAME` | `"SSO"` | Label for the login button: "Sign in with \<name\>". |
| `OIDC_DISCOVERY_URL` | — | Full URL to the provider's `.well-known/openid-configuration` endpoint. **Required** when `OIDC_ENABLED = True`. |
| `OIDC_CLIENT_ID` | — | OAuth 2.0 client ID registered in the provider. |
| `OIDC_CLIENT_SECRET` | — | OAuth 2.0 client secret. Keep this out of version control. |
| `OIDC_SCOPES` | `"openid profile email"` | Space-separated list of OIDC scopes to request. |
| `OIDC_MATCH_CLAIM` | `"preferred_username"` | Which token claim to use when linking a first-time SSO login to an existing local account. Options: `preferred_username`, `email`, `sub`. |
| `OIDC_ROLE_ADMIN` | `"arcology-admin"` | Provider role name that grants admin access and full read/write permission. |
| `OIDC_ROLE_READ_WRITE` | `"arcology-read-write"` | Provider role name that grants full read/write permission. |
| `OIDC_ROLE_READ_ONLY` | `"arcology-read-only"` | Provider role name that grants read-only permission. |
| `OIDC_ROLE_API_ACCESS` | `"arcology-api"` | Provider role name that grants API key creation. |
| `OIDC_REQUIRE_ROLE` | `False` | When `True`, users with no matching permission role are denied login entirely. When `False` (default), they are downgraded to `READ_ONLY` instead. |
| `OIDC_SINGLE_LOGOUT` | `False` | Enable when the identity provider realm is **dedicated to Arcology** (not shared with other applications). When `True`: (1) the **Logout** button also terminates the provider session via RP-Initiated Logout; (2) users denied access are redirected through the provider logout to break the SSO re-authentication loop. Leave `False` for shared corporate SSO realms — logging out of Arcology should not end sessions in other applications. |
| `PERMANENT_SESSION_LIFETIME` | Flask default (31 days) | Session lifetime in seconds for SSO logins. Recommended: `28800` (8 hours). |

---

## Role Mapping

On every SSO login, Arcology collects role strings from the token claims in
this order (all sources are merged):

1. `realm_access.roles` — Keycloak realm-level roles
2. `resource_access.<client_id>.roles` — Keycloak client-level roles
3. Top-level `roles` claim — generic OIDC providers
4. `groups` claim — Azure AD group names, Okta groups

The collected roles are compared against the four `OIDC_ROLE_*` config values.
The **highest matching role wins**:

| Role matched | `is_admin` | `permission` | `can_use_api` |
|---|---|---|---|
| `OIDC_ROLE_ADMIN` | `True` | `READ_WRITE` | follows `OIDC_ROLE_API_ACCESS` |
| `OIDC_ROLE_READ_WRITE` | `False` | `READ_WRITE` | follows `OIDC_ROLE_API_ACCESS` |
| `OIDC_ROLE_READ_ONLY` | `False` | `READ_ONLY` | follows `OIDC_ROLE_API_ACCESS` |
| *(none matched)* | `False` | `READ_ONLY` | `False` |

When no permission role matches, the account is **downgraded to `READ_ONLY`**
on the next login.  This means removing all Arcology roles from the identity
provider immediately takes effect the next time the user authenticates.

To **completely block access** (rather than downgrade to read-only), set
`OIDC_REQUIRE_ROLE = True`.  With this flag, a user with no matching permission
role is denied login with a clear error message rather than being allowed in as
read-only.  Removing all Arcology roles from the user in the identity provider
then revokes their access entirely.

When a user holds **multiple** permission roles, the highest wins: admin
outranks read-write, which outranks read-only.  Having both `arcology-admin`
and `arcology-read-write` assigned is harmless — the result is admin +
READ_WRITE either way.

**Restriction bypass permissions** (malware, PII, copyright, etc.) are always
managed manually in the Arcology admin panel and are never synced from SSO roles.

**Role sync only applies to SSO-managed accounts** — local-only accounts are
never affected.

---

## Account Linking

When a user logs in via SSO for the first time, Arcology resolves their local
account in this order:

1. **Match by `oidc_sub`** (fast path) — if a local account already has this
   subject identifier stored, it is used immediately.
2. **Match by configured claim** — the claim named in `OIDC_MATCH_CLAIM`
   (default: `preferred_username`) is compared against existing local usernames
   (or emails if `OIDC_MATCH_CLAIM = email`).  On a match, the account is
   linked: `oidc_sub` is stored, `oidc_managed` is set to `True`, and the
   account's local password is disabled (set to a sentinel that always fails
   authentication) so that revoking access in the identity provider fully
   removes it.
3. **Auto-provision** — if no match is found, a new local account is created
   using `preferred_username` (truncated to 50 characters) and `email` from the
   token.  The account is marked as SSO-managed and assigned `READ_ONLY`
   permission by default; roles are then applied immediately.

> **Email matching and verification:** When `OIDC_MATCH_CLAIM = email`, Arcology
> only links the account if the token includes `email_verified: true`.  An
> unverified email claim is silently skipped (no link, falls through to
> auto-provision) to prevent a provider that issues unverified addresses from
> being used to take over an existing account.  Ensure your identity provider
> marks emails as verified for all Arcology users.

> **Shared username namespace:** Local accounts and SSO-provisioned accounts
> share the same username namespace.  If an SSO user's `preferred_username`
> matches an existing local account, Arcology links them on first SSO login
> (step 2 above) and that account becomes SSO-managed.  From that point, the
> account's permissions are governed entirely by SSO roles and its local
> password no longer works.  The bootstrap admin account created by
> `flask create-admin` is exempt only as long as no IdP identity presents a
> matching username — keep the bootstrap admin username distinct from any
> identity provider account, or use `OIDC_MATCH_CLAIM = sub` to disable
> claim-based linking entirely.

If auto-provisioning would create a username that already belongs to an unlinked
local account, the login fails with a clear error message.  To resolve this,
either:
- Ask an administrator to set `oidc_sub` on the existing local account (via the
  database or a future admin UI), or
- Rename the conflicting local account before the SSO user logs in.

---

## Provider Setup Examples

### Keycloak

1. In the Keycloak admin console, go to **Clients → Create client**.
2. Set **Client ID** to `arcology` (or your chosen ID).
3. Enable **Client authentication** (confidential client).
4. Set **Valid redirect URIs** to `https://arcology.example.com/auth/sso/callback`.
5. Set **Valid post logout redirect URIs** to `https://arcology.example.com/login`.
   This is required for `OIDC_SINGLE_LOGOUT = True` — Keycloak rejects the
   post-logout redirect if it is not listed here.  You can use `*` to allow any
   URI, but listing the exact URL is safer.
6. On the **Credentials** tab, copy the **Client secret**.
7. Create realm roles: `arcology-admin`, `arcology-read-write`, `arcology-read-only`, `arcology-api`.
8. Assign roles to users or groups via **Users → \<user\> → Role mappings**.

Keycloak emits realm roles in `realm_access.roles` in the access token by
default, but Arcology reads roles from the **ID token**.  You need to enable
this for the ID token using one of the two approaches below.

**Option A — per-client (recommended, does not affect other clients)**

Add a mapper directly to the client's dedicated scope so only the Arcology
client's ID token is changed:

1. **Clients** → select your client (e.g. `arcology-dev`)
2. **Client scopes** tab → click the `arcology-dev-dedicated` entry (labelled
   *Dedicated*)
3. **Add mapper** → **By configuration** → **User Realm Role**
4. Set:
   - **Multivalued**: ON
   - **Token Claim Name**: `realm_access.roles`
   - **Claim JSON Type**: String
   - **Add to ID token**: **ON**
   - **Add to access token**: OFF *(access token already handled by the shared `roles` scope)*
   - **Add to userinfo**: OFF
5. Save.

**Option B — realm-wide (affects all clients using the `roles` scope)**

- **Client scopes** → **roles** → **Mappers** → **realm roles** → enable
  **Add to ID token**.

Without one of these, `realm_access` is absent from the ID token and role sync
produces no changes.

Discovery URL format:
```
https://<keycloak-host>/realms/<realm-name>/.well-known/openid-configuration
```

### Azure AD / Entra ID

1. In the Azure portal, go to **App registrations → New registration**.
2. Set the redirect URI to `https://arcology.example.com/auth/sso/callback`
   (Web platform).
3. Under **Certificates & secrets**, create a new client secret.
4. Under **Token configuration**, add a **Groups claim** (Security groups or
   All groups) so group membership is included in the token.
5. Create security groups in Azure AD named to match your `OIDC_ROLE_*` config
   values (e.g. `arcology-admin`).
6. Assign users to those groups.

Set `OIDC_SCOPES = "openid profile email"` (the default).

Discovery URL format:
```
https://login.microsoftonline.com/<tenant-id>/v2.0/.well-known/openid-configuration
```

> **Note:** Azure AD emits group object IDs by default, not display names.
> Either configure the groups claim to emit display names, or set your
> `OIDC_ROLE_*` config values to the group object IDs instead.

### Okta

1. In the Okta admin console, go to **Applications → Create App Integration**.
2. Choose **OIDC - OpenID Connect** and **Web Application**.
3. Set the **Sign-in redirect URI** to `https://arcology.example.com/auth/sso/callback`.
4. Note the **Client ID** and **Client secret**.
5. Under **Sign On → OpenID Connect ID Token**, add a **Groups claim** with
   filter `Starts with` `arcology-` (or your prefix of choice) so group names
   are included in the token.
6. Create Okta groups named to match your `OIDC_ROLE_*` values and assign users.

Discovery URL format:
```
https://<your-okta-domain>/.well-known/openid-configuration
```

---

## Session Management

SSO logins use Flask's permanent session mechanism with the lifetime controlled
by `PERMANENT_SESSION_LIFETIME` (in seconds).  The recommended value for
enterprise SSO is 8 hours:

```ini
PERMANENT_SESSION_LIFETIME = 28800
```

Local-login sessions use the browser-session lifetime (expire when the browser
closes) unless you set `PERMANENT_SESSION_LIFETIME` globally.

---

## Logout Behaviour

`OIDC_SINGLE_LOGOUT` controls whether Arcology ends the provider session when
a user's access to Arcology ends — either by clicking **Log out** or by being
denied access at login.

**`OIDC_SINGLE_LOGOUT = False` (default)** — use this for shared corporate SSO
realms where the identity provider is also used by other applications.  Clicking
**Log out** clears only the local Arcology session; the provider session remains
active so the user stays logged in to other applications.  If a user is denied
access (e.g. no Arcology roles assigned), they are returned to the login page
but their provider session is untouched.

**`OIDC_SINGLE_LOGOUT = True`** — use this when the identity provider realm is
dedicated to Arcology.  Both the **Log out** button and access-denial redirects
route through the provider's `end_session_endpoint` (OIDC RP-Initiated Logout),
terminating the provider session before returning to the Arcology login page.
This also prevents the SSO re-authentication loop that would otherwise occur
when a user with no Arcology roles is redirected back through an active provider
session on every login attempt.

RP-Initiated Logout requires the provider to support `end_session_endpoint`
(Keycloak does; Azure AD and Okta may require additional configuration).

---

## Migrating Existing Users to SSO

To switch an existing Arcology instance from local-only auth to SSO without
disrupting current users:

1. Configure SSO as described above.
2. Set `LOCAL_LOGIN_ENABLED = True` (keep the local login form visible).
3. Tell users to log in via the SSO button.  On their first SSO login, their
   existing local account will be linked automatically (matched by
   `preferred_username` or `email`, depending on `OIDC_MATCH_CLAIM`).
4. Once all users have linked their accounts, set `LOCAL_LOGIN_ENABLED = False`
   to enforce SSO-only login.

The bootstrap admin account created by `flask create-admin` is a local account
(`oidc_managed = False`).  Its permissions are never touched by SSO role sync.
Keep `LOCAL_LOGIN_ENABLED = True` (or access the database directly) if you ever
need to log in as the local admin when the identity provider is unavailable.

---

## Troubleshooting

**"SSO login failed" flash message on callback**

Check the web container logs (`docker compose logs web`) for the underlying
error.  Common causes:
- `OIDC_CLIENT_SECRET` is wrong or missing.
- The redirect URI in the provider does not exactly match
  `https://arcology.example.com/auth/sso/callback` (including scheme and
  trailing path).
- The provider's discovery URL is unreachable from the web container.

**User gets "Your account is not authorised" error**

The login succeeded but no Arcology account could be resolved or created.
This happens when auto-provisioning is blocked by a username collision.  Check
the web logs for the specific reason and follow the instructions in
[Account Linking](#account-linking).

**Permissions are not updated after changing roles in the provider**

Role sync runs on every SSO login.  The user must log out and log back in via
SSO for the change to take effect.

**Keycloak shows "Invalid Redirect URI" on logout with `OIDC_SINGLE_LOGOUT = True`**

Keycloak 17+ maintains a separate **Valid post logout redirect URIs** list
(distinct from Valid redirect URIs).  If it is empty, Keycloak rejects the
`post_logout_redirect_uri` parameter Arcology sends after logout.

Fix: in the Keycloak admin console, open your client's **Settings** tab and add
`https://arcology.example.com/login` to **Valid post logout redirect URIs**.
See step 5 of the [Keycloak setup](#keycloak) section above.

**`OIDC_SINGLE_LOGOUT = True` but logout doesn't end the provider session**

The provider must expose an `end_session_endpoint` in its discovery document.
Not all providers or configurations support this.  Check the discovery URL
response and your provider's documentation.

**User logs in successfully but permissions / admin flag are not updated**

Arcology reads roles from the **ID token** claims, not the access token.  The
access token (a JWT with `"aud": "account"`) and the ID token carry different
claim sets in Keycloak — `realm_access.roles` is added to the access token by
default but the ID token mapper must be explicitly enabled.

Check: **Client scopes → roles → Mappers → realm roles → Add to ID token** is
**ON** in the Keycloak admin console.  See the [Keycloak setup](#keycloak)
section above for the full step.

To confirm what Arcology actually sees, set `LOG_LEVEL=DEBUG` on the web
container and look for `OIDC role sync for` lines in the logs — they show the
exact role set extracted from the token and the resulting permission values.

**"SSO provider is unavailable" flash or DNS resolution errors in Docker logs**

Docker containers on Linux typically inherit the host's stub resolver address
(`127.0.0.53` from `systemd-resolved`), which is not reachable from inside the
container's network namespace.  If the web container cannot resolve your
Keycloak hostname, add explicit DNS servers to the `web` service in your
`docker-compose.yml` (or a local override file):

```yaml
services:
  web:
    dns:
      - 8.8.8.8
      - 8.8.4.4
```

Then recreate the container: `docker compose up -d --force-recreate web`.

**`preferred_username` claim is an email address**

Some providers (Okta, Azure AD) set `preferred_username` to an email address.
If your local usernames are short names (e.g. `alice`), set
`OIDC_MATCH_CLAIM = email` and ensure local accounts have the `email` column
populated, or switch to `OIDC_MATCH_CLAIM = sub` to always auto-provision fresh
accounts rather than linking by name.
