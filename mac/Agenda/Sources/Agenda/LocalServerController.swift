import AgendaSyncCore
import Combine
import Foundation
import Security

@MainActor
final class LocalServerController: ObservableObject {
    @Published private(set) var activeURL: URL?

    let supervisor: PythonSupervisor
    private var supervisorObservation: AnyCancellable?
    private var cloudCoordinator: AnyObject?

    init() {
        supervisor = PythonSupervisor()
        // ContentView observes this controller, not its nested supervisor.
        // Forward asynchronous status/log changes so startup failures replace
        // the spinner immediately instead of waiting for an unrelated redraw.
        supervisorObservation = supervisor.objectWillChange.sink { [weak self] in
            self?.objectWillChange.send()
        }
    }

    func start() async {
        activeURL = nil
        await supervisor.start()
        if await supervisor.waitUntilReady() {
            activeURL = supervisor.localURL
            startCloudKitIfAvailable()
        }
    }

    func retry() async {
        if supervisor.status != .ready {
            await supervisor.restart()
        }
        if await supervisor.waitUntilReady() {
            activeURL = supervisor.localURL
        }
    }

    func restart() async {
        activeURL = nil
        await supervisor.restart()
        if await supervisor.waitUntilReady() {
            activeURL = supervisor.localURL
        }
    }

    func navigationFailed() {
        activeURL = nil
    }

    func shutdown() {
        supervisor.stop()
    }

    private func startCloudKitIfAvailable() {
        guard
            #available(macOS 14.0, *),
            cloudCoordinator == nil,
            Self.hasAgendaCloudKitEntitlement()
        else { return }
        let coordinator = CloudKitCoordinator()
        cloudCoordinator = coordinator
        coordinator.start(localAPI: supervisor.localURL)
    }

    private static func hasAgendaCloudKitEntitlement() -> Bool {
        guard
            let task = SecTaskCreateFromSelf(nil),
            let value = SecTaskCopyValueForEntitlement(
                task,
                "com.apple.developer.icloud-container-identifiers" as CFString,
                nil
            ),
            let containers = value as? [String]
        else { return false }
        return containers.contains(agendaCloudContainerIdentifier)
    }
}
