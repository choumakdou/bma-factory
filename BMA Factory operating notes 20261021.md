# BMA Factory — operating notes

Short reference for the things that need attention, and the things that
don't. Written 21 October 2026.

---

## 1. The one thing that DOES need doing periodically

### Re-sign in, every 7 days

**Next one due: 28 October 2026.**

Google expires refresh tokens issued by an OAuth client in the **Testing**
publishing status seven days after consent. Google's own documentation
(*Manage App Audience*):

> Authorizations by a test user will expire seven days from the time of
> consent. If your OAuth client requests an `offline` access type and
> receives a refresh token, that token will also expire. The only exception
> to this behavior is if your app requests a subset of the following: name,
> email address, and user profile… If your app requests any other OAuth
> scopes, then this exception does not apply.

We request `drive.readonly` and `drive.file` as well as the profile scopes,
so the exception does not apply and the seven-day clock runs.

**What the app does about it:**

- The green banner on the console shows the expiry date:
  `✓ Signed in as … · sign-in valid to 2026-10-28`
- When the token is finally rejected, the banner changes to
  **"Your Google sign-in has expired"** with a sign-in button — rather
  than an empty file list that looks like the app is broken.
- `/auth/status` reports `expires_on` and `expired`.

**What it costs:** one click. Your files in Drive are untouched; only the
app's permission to read them has lapsed.

### If you'd rather not click every week

Move the OAuth client from **Testing** to **In production** in Google Auth
Platform → **Audience**. In production, refresh tokens do not expire unless
revoked, or unused for about six months.

**The trade-off, honestly:**

| | Testing (now) | In production |
|---|---|---|
| Re-auth | every 7 days | ~never |
| Other people can sign in | no — test users only | yes, anyone with the client id and secret |
| Consent screen | shows a "test user" warning | clean, shows "BMA Factory" |
| Verification needed | no (all scopes non-sensitive) | no (all scopes non-sensitive) |

The client id is already visible in the sign-in URL, so the real gate is the
**client secret**, which lives in the GitHub Actions secret and the container
env — not in the public repo. Publishing makes the tool available to anyone
who has both, and lets the consent screen look professional for clients.

My recommendation: **stay in Testing** while this is your own tool, and
accept the weekly click. Publish it if you want to show it to a client or a
colleague.

---

## 2. The things that need attention once, not periodically

### Rotate the two service account keys

- `service-ac1@bma-factory.iam.gserviceaccount.com` (runtime) — its private
  key was briefly readable from a public diagnostic endpoint on 20 October.
  It has been fixed and the endpoint no longer echoes key material, but the
  key should still be replaced.
- `bma-factory-deployer@bma-factory.iam.gserviceaccount.com` (deploy) — this
  key is in our chat history and in the GitHub Actions secret.

To rotate the runtime key I need `roles/iam.serviceAccountAdmin` on the
deploy service account; the deploy key can be rotated from the console.
Roughly every 6–12 months as good practice, and **immediately** for the
runtime one.

### Fill in the OAuth Branding URLs

Google Auth Platform → **Branding** wants three URLs. All three are live:

- home page — `https://bma-factory-demo-bxbgttiecq-df.a.run.app/`
- privacy policy — `https://bma-factory-demo-bxbgttiecq-df.a.run.app/privacy`
- terms of service — `https://bma-factory-demo-bxbgttiecq-df.a.run.app/terms`

### Tick the profile scopes in Data Access

Google Auth Platform → **Data Access**. `drive.readonly` and `drive.file` are
already checked. Adding `…/auth/userinfo.email` and `openid` makes the
signed-in banner reliable.

---

## 3. The things that need NOTHING

Every cache in the app refreshes itself. There is no cron job, no manual
"clear cache" step, and no scheduled task to run.

| What | How it refreshes | Notes |
|---|---|---|
| Parsed workbooks | keyed on file id + modified time + a digest of the parsing code | re-parsed when the file changes **or** the code changes |
| Drive folder → id map | never expires | invalidated on a miss; safe because moving a folder does not change its id |
| Drive folder list | 15-minute TTL | a folder you create appears in the picker within 15 minutes |
| User profile | 15-minute TTL | |
| Granted scopes | memoised per container | re-read after a sign-in |
| OAuth token | stored in Cloud Storage, survives the container sleeping | refreshed automatically until the 7-day expiry |

**The one caveat:** the Drive folder list is a 15-minute cache, so a folder
you create right now may not appear in the picker on the content page for up
to 15 minutes. The file listing itself is live — a workbook you drop in
`/Intake/` shows up immediately.

---

## 4. Performance, and the cold start

| Page | Time (warm) |
|---|---|
| `/` console | ~1.2 s |
| `/intake` | ~1.2 s |
| `/case/bel-air-m2` | ~0.3 s |
| `/api/cases` | ~0.3 s |

**If a page is slow, it is a cold start.** Cloud Run scales the container to
zero when it is idle (which is what keeps the bill at zero), and the first
request after a quiet spell has to wake it — roughly 15–25 seconds of
Cloud Run's own container start, which no amount of code changes will
remove. Every page after that is fast.

To remove the cold start you would set `min-instances: 1`, which keeps one
instance warm around the clock. **That costs roughly $65 a month** at
1 vCPU / 512 MiB, against a free tier of 180,000 vCPU-seconds (about
four days of always-on). Keeping scale-to-zero costs nothing, so the
trade is: $0 and a slow first load, or ~$65/month and never a slow one.

**Recommendation: keep scale-to-zero.** The cold start only bites on the
first load of a session.
