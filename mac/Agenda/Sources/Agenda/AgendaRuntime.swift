import Foundation

/// How this copy of Agenda runs.
///
/// `scripts/try-first-run.sh` builds "Agenda Sandbox.app", whose Info.plist
/// sets `AgendaSandbox` to true. A sandbox copy is a completely separate,
/// throwaway installation, so the first-run experience can be tried on a Mac
/// that already runs Agenda:
///
/// - its own data folder (`Application Support/Agenda Sandbox`) and port
///   (8562), so it never opens, seeds, backs up, or stops the real database
///   or server;
/// - no iCloud sync, update checks, launch-at-login, or keychain cleanup,
///   even if it were somehow signed with those capabilities.
///
/// It also has its own bundle ID, which keeps its preferences, web storage,
/// and single-instance guard apart from the real app.
enum AgendaRuntime {
    static let isSandbox: Bool =
        Bundle.main.object(forInfoDictionaryKey: "AgendaSandbox") as? Bool ?? false

    static let port: Int = isSandbox ? 8562 : 8552

    static let dataFolderName: String = isSandbox ? "Agenda Sandbox" : "Agenda"
}
