# Permissions & Access

---

## User Permission Tiers

Every Arcology account has a permission level that controls what it can do.  Tiers are ordered — a higher tier inherits everything the lower tiers can do.

| Tier | Who it's for | Can browse | Can upload & edit | Can manage taxonomy & databases |
|------|-------------|:----------:|:-----------------:|:-------------------------------:|
| **Read Only** | Guests, researchers, patrons | ✓ | ✗ | ✗ |
| **Read/Write** | Curators responsible for managing the collection | ✓ | ✓ | ✓ |
| **Staff** | Trusted operators with additional queue management capabilities | ✓ | ✓ | ✓ |
| **Admin** | System administrators | ✓ | ✓ | ✓ + user management |

> **Staff** tier users can additionally:
>
> - **Reset stuck analysis jobs** — the *Reset stale* button on the Analysis Queue page (and the matching API endpoint) requires Staff or above.  This is intentionally restricted so that ordinary curators cannot accidentally disrupt the global analysis queue.
> - **Raise re-analysis priority** without needing the per-user *Can prioritise analyses* grant (Read/Write users can also raise priority if an admin has set that flag on their account).
>
> Staff cannot manage user accounts; that still requires Admin.

### Admin vs. Read/Write

Admin status (`is_admin`) is independent of the permission tier.  An account can be admin with Read Only permission (can manage users but not upload), or Read/Write without admin (can upload but not manage users).  In normal deployments the admin account has both.

---

## Private Items and Sharing

Items can be marked **Private**.  A private item is hidden from all users except:

- The item's owner
- Users with whom the item has been explicitly shared
- Admins

**Privacy is inherited** — marking a parent item private automatically makes all its children and artefacts private too.  Making a child item private does not affect the parent.

### Sharing

To share a private item, open the item and use the **Share** section:

- **Viewer** access lets the recipient browse and download the item.
- **Curator** access additionally allows editing, uploading artefacts, and managing analyses.

You can share with individual users or with **groups**.  Groups are managed by admins under **Admin** → **Groups**.

---

## Download Restrictions

Some artefacts carry download restrictions that prevent users from downloading the file.  Restrictions are recorded per artefact (or per extracted file within an artefact) with a type and an optional reason.

| Restriction type | Typical use |
|-----------------|-------------|
| `Malware` | File contains known malicious software |
| `PII` | Personal identifiable information |
| `Copyright` | Download restricted for copyright reasons |
| `Legal hold` | File under legal hold; do not distribute |
| `Explicit` | Content not suitable for unrestricted distribution |
| `Corrupted` | File is corrupted and should not be distributed |

Restricted artefacts show a **Restricted** button instead of a Download button.  Clicking it shows the restriction reason.

### Bypass Permissions

Administrators can grant specific users the ability to override restrictions on a per-type basis.  A user with a MALWARE bypass, for example, can download malware-restricted artefacts after confirming the action.  This is useful for security researchers or administrators who need access to restricted content.

Contact your Arcology administrator if you need bypass access to a specific restriction type.

---

## API Keys

API keys let you access Arcology from the `arco` CLI tool or directly via the REST API without using your password.

To create a key, go to **Profile** (your username in the top-right corner) and click **Generate API Key**:

1. Give the key a memorable name.
2. Choose a permission level.
3. Click **Generate**.
4. **Copy the key immediately** — it is shown only once.  Only a cryptographic hash is stored after you leave this page.

### Key Permission Levels

| Level | Can do |
|-------|--------|
| **Read Only** | List and download items and artefacts |
| **Read/Upload** | Read Only + upload new artefacts |
| **Read/Write** | Full read, write, and management access |

A key's effective permission is capped by your account's permission tier.  If your account is Read Only, any key you create will also be Read Only regardless of what level you choose.

### Key Security

- Each key starts with a short prefix (e.g. `ARC-abc123`) shown in the key list so you can identify it without exposing the full value.
- Keys can be revoked at any time from the Profile page.
- If you suspect a key has been compromised, revoke it immediately and generate a replacement.

---

## OIDC / SSO Accounts

If your deployment uses Single Sign-On (OIDC), your account is managed by the identity provider.  You cannot change your password in Arcology — use your SSO provider's password reset flow instead.

SSO users may have their permission tier controlled by roles assigned in the identity provider.  Your administrator can tell you which SSO roles map to which Arcology permission levels.

---

## Public Mode

When **Public Mode** is enabled by the administrator, anonymous visitors can browse and search the collection without logging in.  Private items are still hidden from anonymous visitors.

If **Public Downloads** is also enabled (the default when Public Mode is on), anonymous visitors can download unrestricted artefacts.  If Public Downloads is disabled, anonymous visitors can browse metadata but must log in to download files.

Write access (uploading, editing, deleting) always requires a logged-in account regardless of public mode settings.
