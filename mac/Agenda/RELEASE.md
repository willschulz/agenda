# Releasing Agenda for macOS

How to produce a DMG that installs on someone else's Mac without a Gatekeeper
warning. Two of the steps need the Apple portal and a GUI, so they are yours;
everything after that is one command.

These steps are for whoever maintains the official build (Team ID
`NK3J94B5PX`). Contributors don't need them: `install.sh` builds an ad-hoc
signed copy for local testing. A fork that ships its own build needs its own
Apple Developer team, bundle ID, CloudKit container, and Sparkle key.

## What state we are in

The pipeline is built and validated end to end. Developer ID and notarytool
credentials are configured, and Sparkle 2.9.5 provides automatic updates from
the public releases-only repository `willschulz/agenda-releases`.

| Piece | State |
|---|---|
| Inside-out hardened-runtime signing | Working; all bundled Mach-O files (Python runtime/extensions and the local-AI helper), Sparkle's nested XPC/app/framework bundles, then the app |
| Entitlements | Normal release: `Agenda.entitlements`; private-sync builds use `AgendaDevelopment.entitlements` or gated `AgendaCloudKitProduction.entitlements`; `python.entitlements` is intentionally empty |
| DMG packaging | Working; ~43 MB, with an `Applications` symlink for drag-install |
| Library validation under hardened runtime | Verified passing with a real Team ID |
| Developer ID Application certificate | Configured (`NK3J94B5PX`) |
| notarytool credentials | Configured (`agenda-notary`) |
| Sparkle EdDSA private key | Login Keychain account `ed25519` |
| Public update feed | `https://willschulz.github.io/agenda-releases/appcast.xml` |

## One-time setup — Developer ID Application certificate

The keychain currently holds only an **Apple Development** certificate. That is
enough to sign and run locally, but Apple will not notarize it and Gatekeeper
will not trust it on another Mac. You need a **Developer ID Application**
certificate, which is a different type.

Easiest route, through Xcode:

1. Open Xcode, then **Settings** (Cmd-,) and the **Accounts** tab.
2. Select the maintainer's Apple ID in the left column.
3. Click **Manage Certificates…** (bottom right).
4. Click **+** (bottom left) and choose **Developer ID Application**.
5. Wait for it to appear in the list, then close the sheet.

This requires your Apple ID to hold the **Account Holder** or **Admin** role on
team `NK3J94B5PX`. If **Developer ID Application** is greyed out or missing from
the **+** menu, that role is the reason, not a bug — check
[developer.apple.com/account](https://developer.apple.com/account) under People.

Confirm it worked:

```bash
security find-identity -v -p codesigning
```

You should see a new line reading `Developer ID Application: Will Schulz
(NK3J94B5PX)` alongside the existing Apple Development entry. The release script
auto-detects it by that name; no configuration needed.

## One-time setup — Store notarization credentials

Notarization uploads the app to Apple for automated malware scanning. It
authenticates with an app-specific password, not your Apple ID password.

1. Go to [appleid.apple.com](https://appleid.apple.com) and sign in.
2. Under **Sign-In and Security**, choose **App-Specific Passwords**.
3. Click **+**, name it something like `agenda-notary`, and copy the generated
   password. You cannot view it again after closing the dialog.
4. Hand it to `notarytool`, which stores it in the login keychain:

```bash
xcrun notarytool store-credentials "agenda-notary" \
  --apple-id <your-apple-id> \
  --team-id NK3J94B5PX
```

Paste the app-specific password when prompted. The profile name `agenda-notary`
is what `package-release.sh` looks for by default.

Confirm it is stored and working:

```bash
xcrun notarytool history --keychain-profile "agenda-notary" >/dev/null \
  && echo "notary profile present and working"
```

(`security find-generic-password` cannot see this credential — notarytool
profiles live in the Local Items/iCloud keychain, not the login keychain's
ordinary generic-password store, so `security` will always report it missing
even when it works. Ask notarytool, not `security`.)

## Step 1 — Cut the release

```bash
cd mac/Agenda
./scripts/package-release.sh
```

That is the whole release. It builds, assembles, signs inside-out with hardened
runtime, notarizes and staples the app, builds and signs the DMG, notarizes and
staples that too, and finishes with a Gatekeeper assessment.

Notarization is the slow part: usually one to five minutes per submission, and
there are two (app and DMG). `notarytool --wait` blocks until Apple answers, so
the script may sit quietly for a while. That is normal.

Artifacts land in `dist/`:

```
dist/Agenda-0.2.0-arm64.dmg    <- the file you send
dist/stage/Agenda.app          <- the signed, stapled app
```

Useful variations:

```bash
./scripts/package-release.sh --skip-notarize   # sign and package only, no upload
./scripts/package-release.sh --identity -      # ad-hoc dry run (see caveat below)
./scripts/package-release.sh --no-build        # reuse the existing binary
```

CloudKit is an advanced capability, so a signed Mac sync build must also embed
the matching provisioning profile:

```bash
./scripts/package-release.sh \
  --entitlements AgendaDevelopment.entitlements \
  --provisioning-profile ~/Library/Developer/Xcode/UserData/Provisioning\ Profiles/<profile>.provisionprofile \
  --skip-notarize
```

Use `AgendaCloudKitProduction.entitlements` only with a Developer ID CloudKit
profile and only after the private CloudKit schema has been explicitly approved
for Production. The normal release command remains unchanged and does not
silently enable CloudKit. At runtime, Agenda checks for the configured iCloud
container entitlement before constructing `CKContainer`; ad-hoc `install.sh`
and normal non-sync release builds skip the CloudKit coordinator while keeping
the localhost app fully functional.

## Step 2 — Verify before publishing

```bash
# 1. Notarized Developer ID, not ad-hoc:
codesign -dv --verbose=2 dist/stage/Agenda.app 2>&1 | grep -E 'TeamIdentifier|flags'
#    Expect: TeamIdentifier=NK3J94B5PX
#            flags=0x10000(runtime)        <- "runtime" present, "adhoc" absent

# 2. Gatekeeper accepts the DMG:
spctl -a -vvv -t install dist/Agenda-0.2.0-arm64.dmg
#    Expect: accepted
#            source=Notarized Developer ID

# 3. The stapled ticket is attached (works offline):
xcrun stapler validate dist/Agenda-0.2.0-arm64.dmg
#    Expect: The validate action worked!
```

The test that actually predicts a recipient's experience is a quarantined download,
because that is the only path where Gatekeeper's first-launch check runs:

```bash
cp dist/Agenda-0.2.0-arm64.dmg /tmp/quarantine-test.dmg
xattr -w com.apple.quarantine "0083;00000000;Safari;" /tmp/quarantine-test.dmg
open /tmp/quarantine-test.dmg
# Drag Agenda.app to /Applications, launch it, and confirm there is no
# "unidentified developer" or "Apple could not verify" dialog.
```

Then confirm the app actually works from that copy:

```bash
pgrep -lx Agenda
curl -sS -o /dev/null -w 'HTTP %{http_code}\n' http://127.0.0.1:8552/api/projects
```

## Step 3 — Publish the update

Write short Markdown release notes, then publish the already-notarized DMG:

```bash
./scripts/publish-release.sh --notes /path/to/release-notes.md
```

The publisher verifies Gatekeeper and the stapled ticket, runs the agent-pack
privacy/parity checks, uploads the DMG plus
`Agenda-agent-integration-<version>.zip` to a GitHub Release in
`willschulz/agenda-releases`, generates an EdDSA-signed Sparkle entry using the
private key in the login Keychain, and pushes `docs/appcast.xml` to GitHub
Pages. It is safe to rerun for the same version: release assets are replaced
and the appcast is updated only if needed.

Verify the public endpoints after GitHub Pages deploys:

```bash
curl -fsS https://willschulz.github.io/agenda-releases/appcast.xml
gh release view "v$(/usr/libexec/PlistBuddy \
  -c 'Print :CFBundleShortVersionString' Sources/Agenda/Info.plist)" \
  --repo willschulz/agenda-releases
```

The first Sparkle-bearing release requires one final manual install for users
of 0.2.0. Later releases appear through **Agenda → Check for Updates…**.

## Version bumping

Two keys in `Sources/Agenda/Info.plist`, and they do different jobs:

- `CFBundleShortVersionString` is the human-facing version (`0.2.0`) and names
  the DMG.
- `CFBundleVersion` is the build number. Sparkle (Phase D) compares this to
  decide whether an update exists, so it **must increase on every release you
  publish**, even a rebuild of the same marketing version.

## Troubleshooting

**`no Developer ID Application certificate found`, and it falls back to ad-hoc.**
Step 1 did not complete. `security find-identity -v -p codesigning` will show
only the Apple Development line.

**`Failed to parse entitlements: AMFIUnserializeXML: syntax error near line N`.**
An entitlements file is not well-formed XML. The usual cause is a double hyphen
inside an XML comment, which is invalid XML — `plutil -lint` accepts it but
codesign does not. The script's preflight now catches this before signing
anything; if you hit it, fix line N in the named `.entitlements` file.

**The app launches but hangs on "Starting local Agenda…", and a manual run of
the bundled interpreter reports `code signature ... not valid for use in
process: ... have different Team IDs`.** This is library validation rejecting
the bundled Python extensions. Diagnose it directly:

```bash
cd dist/stage/Agenda.app/Contents/Resources/server
../python/bin/python3 -c 'import uvicorn, fastapi, pydantic_core; print("ok")'
```

Expected with a real signing identity: `ok`. **This failure is normal and
expected for an ad-hoc (`--identity -`) build** — ad-hoc signatures have no Team
ID, so validation cannot match one, and the app genuinely will not start. It is
not a packaging defect. Any identity with a real Team ID (including the Apple
Development certificate) makes it pass, which is how we confirmed
`python.entitlements` needs no exceptions.

If it ever fails with a *real* Developer ID identity, then and only then add to
`python.entitlements`:

```xml
<key>com.apple.security.cs.disable-library-validation</key>
<true/>
```

**Notarization rejected.** Get the reasons; the submission ID is in the script's
output:

```bash
xcrun notarytool log <submission-id> --keychain-profile agenda-notary
```

The likely causes here are a nested binary missing hardened runtime or missing a
secure timestamp. The script applies both to every Mach-O file it finds, and it
finds them by reading magic bytes rather than trusting file extensions, so a new
dependency shipping an extensionless binary should still be caught.

**`resource fork, Finder information, or similar detritus not allowed`.**
Extended attributes on the bundle. `scripts/assemble-app.sh` runs `xattr -cr`,
so this should not recur; if it does, the source tree picked up attributes after
assembly.

## Known limitation: arm64 only

The pinned CPython runtime is `aarch64-apple-darwin`, and
`scripts/vendor-python.sh` hard-errors on anything else. **The DMG will not run
on an Intel Mac** — not slowly, not at all.

Check which Mac a recipient has before sending a build. Adding x86_64 means
a second pinned CPython archive and a second bundle, so it is real work, not a
flag.
