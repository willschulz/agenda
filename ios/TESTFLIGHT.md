# Agenda for iOS: internal TestFlight pilot

These are maintainer notes for the official app's internal TestFlight pilot.
You don't need them to build or test the iOS app; see the iOS section of the
[top-level README](../README.md#ios-app).

The pilot syncs through each user's own private iCloud database. Agenda has
no account service, public database, shared CloudKit database, analytics SDK,
or developer-operated backend. Each tester's records stay in that tester's
CloudKit private database.

## Build identity

- Bundle ID: `com.willschulz.Agenda.iOS`
- CloudKit container: `iCloud.com.willschulz.Agenda`
- Minimum iOS: 17
- Export options: `TestFlightExportOptions.plist`

The archive declares the Production CloudKit environment on purpose, because
TestFlight can't reach Development. Creating the archive does **not** deploy
the CloudKit schema or upload a build.

## Gates before upload

Don't upload or assign a build until all of these are done:

1. Review the Development schema in CloudKit Console.
2. Deploy that schema to Production.
3. Re-verify the matching Production-entitled Mac build after any code
   changes.
4. Confirm each tester has accepted their App Store Connect invitation and
   belongs to the internal testing group.

After that:

```bash
PATH=/usr/bin:/bin:/usr/sbin:/sbin xcodebuild -exportArchive \
  -archivePath dist/AgendaMobile-<version>.xcarchive \
  -exportPath dist/testflight-export \
  -exportOptionsPlist TestFlightExportOptions.plist \
  -allowProvisioningUpdates
```

The restricted `PATH` keeps Xcode from mixing Apple's rsync client with a
Homebrew rsync, a mix that fails the IPA copy step.

Uploading and assigning the exported build is a separate action. Internal
testers don't need Beta App Review, but build processing, agreements, account
roles, or certificate creation can still hold up availability.

## App Store Connect metadata

- **Beta description:** Native offline Agenda viewer using the tester's private
  iCloud database. This pilot validates private Mac/iPhone synchronization.
- **What to test:** Confirm projects and todos appear after the first sync.
  Turn on Airplane Mode and relaunch to confirm the cached board stays
  readable. Edit only the todo inside `CloudKit Sync Test`, then confirm it
  appears in Agenda on the matching Mac. Don't put sensitive information in
  the fixture.
- **Feedback email:** use the App Store Connect account contact.
- **Export compliance:** no custom or non-exempt encryption; networking uses
  Apple's CloudKit/HTTPS stack.
- **Sign-in:** no Agenda sign-in. The tester must be signed into iCloud.
- **Privacy:** CloudKit private database only. No cross-user sharing, tracking,
  analytics, or developer collection.

## Tester setup

1. Install the approved Production-entitled Mac build and launch it while
   signed into the tester's own iCloud account.
2. Confirm the tester's local SQLite database and localhost API keep working
   when CloudKit is unavailable.
3. Install the internal TestFlight build under the same iCloud account.
4. Confirm the tester sees only data seeded from their own Mac. No other
   user's records should ever appear.
