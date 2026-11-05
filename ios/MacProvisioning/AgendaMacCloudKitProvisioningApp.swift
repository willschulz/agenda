import SwiftUI

/// This target is never installed. Building it lets Xcode create and refresh
/// the Development provisioning profile for the existing non-Xcode Agenda Mac
/// bundle, whose real build remains SwiftPM + assemble-app.sh.
@main
struct AgendaMacCloudKitProvisioningApp: App {
    var body: some Scene {
        WindowGroup {
            Text("Agenda CloudKit provisioning helper")
        }
    }
}

