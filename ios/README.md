# ROMmates for iOS

Native SwiftUI client for non-administrator ROMmates accounts. Tabs are selected from
the authenticated account's existing roles: Library for everyone, Devices for Member,
Uploads for Contributor, plus Inbox and Account. Role names and authorization remain
owned by the server.

## Current checkpoint

- iOS 17+, Swift 6, no third-party client dependencies.
- Public HTTPS servers only; session tokens are stored in the iOS Keychain.
- Library browsing, metadata, authenticated artwork, per-game downloads, device
  selections, staged-capacity feedback, device apply/discard, Syncthing delivery
  progress, contributor uploads, inbox, push preferences, and account editing.
- Push registration uses the production entitlement in Release builds and development
  in Debug builds.
- `swiftc -typecheck` passes against the iPhoneOS SDK. A complete Xcode build and
  simulator visual review still need to be run from a normal macOS/Xcode session;
  the Codex sandbox could not access CoreSimulator's asset compiler.

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
ROMMATES_APNS_KEY_PATH=/data/secrets/AuthKey_XXXXXXXXXX.p8
ROMMATES_APNS_KEY_ID=XXXXXXXXXX
ROMMATES_APNS_TEAM_ID=XXXXXXXXXX
ROMMATES_APNS_BUNDLE_ID=com.rommates.app
ROMMATES_APNS_ENVIRONMENT=production
```

An APNs token-signing key from another app can be reused when it belongs to the same
Apple Developer team and has APNs access. Do not commit the key. TestFlight always
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

OAuth/OIDC remains a later migration. The client talks through a small session layer,
so Authorization Code with PKCE can replace password session creation without changing
feature APIs or role checks.
