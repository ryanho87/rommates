# ROMmates iOS server launch checklist

The native app should not require removing Cloudflare Access from the existing browser
and administrator hostname. Give the app a separate hostname with a deliberately smaller
server surface, while keeping the origin reachable only through the existing proxy or
Cloudflare Tunnel.

## 1. Server boundary

- [x] Issue opaque native sessions from `POST /api/v1/mobile/session`.
- [x] Store only a hash of each session token in SQLite.
- [x] Tag sessions as `web` or `mobile` on the server.
- [x] Reject administrator accounts from native login.
- [x] Restrict configured mobile hostnames to an explicit native route allowlist.
- [x] Reject web sessions, browser cookies, the bootstrap token, and anonymous proxy
  access on the mobile hostname.
- [x] Continue applying role and device-ownership authorization after the host allowlist.
- [ ] Replace the process-local login limiter with durable per-account and per-source
  throttling that survives container restarts.
- [ ] Add an account-facing native-session list with individual and all-device revocation.
- [ ] Add MFA or passkey support before broadening access beyond a small trusted group.

## 2. Cloudflare and reverse proxy

- [ ] Leave `rommates.orb-ranch.com` and its full web/admin UI behind Cloudflare Access.
- [ ] Create a separate proxied hostname, for example `rommates-api.orb-ranch.com`.
- [ ] Route that hostname to the same ROMmates container over the private Docker network.
- [ ] Set `ROMMATES_MOBILE_PUBLIC_HOSTS=rommates-api.orb-ranch.com` in the deployed
  Compose environment and recreate the container.
- [ ] Keep `ROMMATES_ALLOW_ANONYMOUS=false`.
- [ ] Ensure the container/origin port cannot be reached directly from the public internet.
- [ ] Add Cloudflare rate limiting for `POST /api/v1/mobile/session` and upload endpoints.
- [ ] Add conservative request/body limits at the proxy that remain compatible with
  ROMmates' resumable upload chunks.
- [ ] Do not use an Access Bypass policy for the full existing application.
- [ ] Do not embed the bootstrap token or a Cloudflare service token in the iOS app.

For a Traefik deployment, the repository includes `compose.traefik.yaml`. Apply it as
an overlay to the base Compose file, or copy its `rommates-mobile` router labels into a
deployment-specific Compose file. The API router intentionally points to the same
container; the application-level hostname boundary is what removes the web and admin
routes from that hostname.

## 3. Boundary verification

Run these checks against the dedicated API hostname before inviting users:

- [ ] Unauthenticated `GET /api/games` returns `401`.
- [ ] A valid non-admin native login succeeds and returns a mobile session token.
- [ ] That token can access the Library and only devices owned by its account.
- [ ] The bootstrap bearer token returns `401` on the mobile hostname.
- [ ] A normal browser session returns `401` on the mobile hostname.
- [ ] `/`, `/mcp`, `/api/status`, `/api/users`, `/api/jobs`, and `/api/trash` return `404`
  on the mobile hostname.
- [ ] Allowed responses include `X-ROMmates-Surface: mobile`.
- [ ] Forced temporary-password replacement works in the app.
- [ ] Disabled users and password changes invalidate existing sessions.
- [ ] Upload, download-ticket, device staging/apply, inbox, and account flows pass with
  Viewer, Contributor, Member, and combined-role test accounts.

## 4. APNs notifications

- [ ] Mount the Apple `.p8` signing key read-only; never commit it or copy it into the app.
- [ ] Configure `ROMMATES_APNS_KEY_PATH`, `ROMMATES_APNS_KEY_ID`,
  `ROMMATES_APNS_TEAM_ID`, `ROMMATES_APNS_BUNDLE_ID`, and production APNs environment.
- [ ] Confirm the Xcode bundle identifier and Push Notifications entitlement match.
- [ ] Test registration, preference updates, delivery, logout unregister, and invalid-token
  cleanup on a physical iPhone.

## 5. App and TestFlight UAT

- [ ] Point the app at the dedicated API hostname, not the full browser/admin hostname.
- [ ] Confirm TLS uses a publicly trusted certificate and no certificate exceptions.
- [ ] Verify tokens persist in Keychain, survive relaunch, and disappear on logout.
- [ ] Test slow/offline networking, expired sessions, server errors, interrupted uploads,
  oversized uploads, insufficient device capacity, and Syncthing delivery progress.
- [ ] Test every supported role combination without using an administrator account.
- [ ] Complete a physical-device archive and internal TestFlight pass.

## 6. Rollout and rollback

- [ ] Start with the owner account and one trusted non-admin account.
- [ ] Review authentication failures, upload activity, device changes, and notification
  delivery during the pilot.
- [ ] Expand only after the boundary tests and role UAT pass.
- [ ] To roll back public native access, remove the API DNS/router and clear
  `ROMMATES_MOBILE_PUBLIC_HOSTS`; the Access-protected web application remains available.
