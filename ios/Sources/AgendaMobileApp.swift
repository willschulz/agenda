import SwiftUI

@main
struct AgendaMobileApp: App {
    @StateObject private var sync = MobileSyncController()

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(sync)
                .task {
                    sync.start()
                }
        }
    }
}

