# ROMmates for iOS

Native SwiftUI client for non-administrator ROMmates accounts. Tabs are selected from
the authenticated account's existing roles: Library for everyone, Devices for Member,
Uploads for Contributor, plus Inbox and Account. Role names and authorization remain
owned by the server.

## Current checkpoint

- iOS 17+, Swift 6, no third-party client dependencies.
- Public HTTPS servers only; session tokens are stored in the iOS Keychain.
- Paginated Library browsing, metadata, authenticated artwork, per-game and device-roster downloads,
  device groups and shared rosters, device selections, visible device filtering and sorting,
  filesystem-backed storage and per-platform metrics,
  staged-capacity feedback,
  device apply/discard, Syncthing delivery
  progress, contributor uploads, inbox, push preferences, account editing, and
  server-driven TestFlight announcements and release notes.
- Push registration uses the production entitlement in Release builds and development
  in Debug builds.
- Debug and optimized Release device builds pass with Xcode. Physical-device behavior,
  including production APNs delivery, is exercised through the internal TestFlight group.

## Generate and open the project

Install XcodeGen, then run:

```sh
cd ios
xcodegen generate
open ROMmates.xcodeproj
```

Before archiving, set the Apple Developer team and confirm the bundle identifier in
`project.yml`. If the identifier changes, use the same value for
`ROMMATES_APNS_BUNDLE_ID` on the server and regenerate the project.

## APNs relay configuration

The provider runs inside the existing ROMmates FastAPI deployment. There is no shared
hosted relay, and the Mac does not need to remain online. Mount the APNs `.p8` key
read-only in the server's existing data volume and set:

```dotenv
ROMMATES_APNS_KEY_PATH=/data/secrets/AuthKey_XS7BLQULZC.p8
ROMMATES_APNS_KEY_ID=XS7BLQULZC
ROMMATES_APNS_TEAM_ID=2NKFMTXAX5
ROMMATES_APNS_BUNDLE_ID=com.rommates.app
ROMMATES_APNS_ENVIRONMENT=production
```

These values deliberately reuse Boba Tracker's APNs provider key. ROMmates uses its
own `com.rommates.app` APNs topic. Do not commit the private key. TestFlight always
uses the production APNs environment.

The server keeps installation tokens and a durable delivery outbox in SQLite. APNs
`410`/invalid-token responses disable the installation; rate limits and temporary
server failures use bounded exponential retry.

## TestFlight handoff

1. Install the updated Python runtime requirements and deploy the database migration.
2. Configure APNs on the server and confirm its public HTTPS URL.
3. Set the Xcode development team and create/confirm the App ID with Push Notifications.
4. Build and run on a physical iPhone, because the simulator does not receive normal
   remote APNs delivery.
5. Verify Viewer, Contributor, and Member role combinations and the forced temporary
   password flow.
6. Archive the Release configuration, validate the production push entitlement, and
   upload the build to App Store Connect for internal TestFlight testing.

## Publish a TestFlight release

After App Store Connect reports the uploaded build as valid and it is available to the
internal group, publish the matching metadata through the full administrator hostname:

```sh
curl --fail-with-body --request POST https://rommates.example.com/api/mobile/releases \
  --header 'Authorization: Bearer YOUR_ROMMATES_ACCESS_TOKEN' \
  --header 'Content-Type: application/json' \
  --data '{"build":5,"version":"1.0","notes":"Release notes for this build."}'
```

Publishing a build for the first time fans one announcement per active native user through
the existing durable APNs queue. New-build announcements stay out of Inbox: tapping the push
or an in-app update banner opens TestFlight, and the complete release notes appear once after
the new build is installed. Re-publishing the same build can correct its notes without
notifying everyone again. The app checks at sign-in and whenever it returns to the foreground.
Keep the complete notes in `ios/releases/` and use the same copy for TestFlight’s
**What to Test** field.

OAuth/OIDC remains a later migration. The client talks through a small session layer,
so Authorization Code with PKCE can replace password session creation without changing
feature APIs or role checks.

Before exposing the server or shipping a TestFlight build, complete the
[iOS server launch checklist](../docs/IOS_SERVER_LAUNCH_CHECKLIST.md). The recommended
deployment keeps the full browser/admin host behind Cloudflare Access and gives the app
a separate hostname protected by ROMmates' mobile-only route boundary.
