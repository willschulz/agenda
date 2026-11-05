import Foundation
import Darwin

enum PythonStatus: Equatable {
    case stopped
    case starting
    case ready
    case error(String)

    var message: String {
        switch self {
        case .stopped: return "Stopped"
        case .starting: return "Starting local Agenda…"
        case .ready: return "Local Agenda ready"
        case .error(let message): return message
        }
    }
}

/// Owns the bundled Python/uvicorn child process. No host Python or shell PATH
/// is used: every executable and source path comes from the app bundle.
@MainActor
final class PythonSupervisor: ObservableObject {
    @Published private(set) var status: PythonStatus = .stopped
    @Published private(set) var logLines: [String] = []

    let port = AgendaRuntime.port
    var localURL: URL { URL(string: "http://127.0.0.1:\(port)/")! }

    private var process: Process?
    private var healthTimer: Timer?
    private var launchGeneration = 0

    init() {}

    func start() async {
        guard process == nil else { return }
        status = .starting
        launchGeneration += 1
        let generation = launchGeneration

        await terminateRecordedStaleProcess()

        guard let paths = bundledPaths() else {
            status = .error("Embedded Python or Agenda server is missing from this app bundle.")
            return
        }

        let databaseURL: URL
        do {
            databaseURL = try prepareDatabase(pythonURL: paths.python, serverURL: paths.server)
        } catch {
            status = .error(error.localizedDescription)
            return
        }

        let child = Process()
        let stdout = Pipe()
        let stderr = Pipe()
        child.executableURL = paths.python
        child.arguments = [
            "-m", "uvicorn", "main:app",
            "--host", "127.0.0.1",
            "--port", "\(port)",
        ]
        child.currentDirectoryURL = paths.server
        child.standardOutput = stdout
        child.standardError = stderr
        child.environment = serverEnvironment(databaseURL: databaseURL)

        attach(stdout, label: "stdout", generation: generation)
        attach(stderr, label: "stderr", generation: generation)
        child.terminationHandler = { [weak self] terminated in
            DispatchQueue.main.async {
                guard let self, generation == self.launchGeneration else { return }
                self.process = nil
                self.stopHealthTimer()
                if self.status == .starting || self.status == .ready {
                    self.status = .error("Local server exited (code \(terminated.terminationStatus)).")
                }
            }
        }

        do {
            // Assign before run(): a very early child exit can otherwise race
            // the termination handler and leave a dead Process stored here.
            process = child
            try child.run()
            writePIDFile(child.processIdentifier)
            appendLog("Started bundled Python (writable)")
            startHealthTimer()
        } catch {
            process = nil
            status = .error("Could not launch local server: \(error.localizedDescription)")
        }
    }

    func stop() {
        launchGeneration += 1
        stopHealthTimer()
        if let process, process.isRunning {
            process.terminate()
        }
        process = nil
        removePIDFile()
        status = .stopped
    }

    /// App termination cannot await uvicorn's graceful shutdown. Kill the child
    /// synchronously so it cannot outlive a normal app quit and retain the DB.
    func forceStop() {
        launchGeneration += 1
        stopHealthTimer()
        if let process, process.isRunning {
            Darwin.kill(process.processIdentifier, SIGKILL)
        }
        process = nil
        removePIDFile()
        status = .stopped
    }

    func restart() async {
        let old = process
        stop()
        if let old {
            let deadline = Date().addingTimeInterval(2)
            while old.isRunning, Date() < deadline {
                try? await Task.sleep(nanoseconds: 100_000_000)
            }
            if old.isRunning {
                appendLog("Graceful restart timed out; force-stopping local server.")
                Darwin.kill(old.processIdentifier, SIGKILL)
            }
            await withCheckedContinuation { continuation in
                DispatchQueue.global(qos: .userInitiated).async {
                    if old.isRunning { old.waitUntilExit() }
                    continuation.resume()
                }
            }
        }
        await start()
    }

    func waitUntilReady(timeout: TimeInterval = 15) async -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if status == .ready { return true }
            if case .error = status { return false }
            try? await Task.sleep(nanoseconds: 150_000_000)
        }
        if status == .starting {
            stopHealthTimer()
            status = .error(
                "The local server did not become ready within \(Int(timeout)) seconds."
            )
            appendLog("Startup timed out after \(Int(timeout)) seconds.")
        }
        return false
    }

    func killStaleServer() async {
        let finder = Process()
        let pipe = Pipe()
        finder.executableURL = URL(fileURLWithPath: "/usr/sbin/lsof")
        finder.arguments = ["-nP", "-tiTCP:\(port)", "-sTCP:LISTEN"]
        finder.standardOutput = pipe
        try? finder.run()
        finder.waitUntilExit()
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        let pids = String(data: data, encoding: .utf8)?
            .split(whereSeparator: \.isNewline)
            .compactMap { Int32($0) } ?? []
        for pid in pids { Darwin.kill(pid, SIGTERM) }
        appendLog("Stopped \(pids.count) stale process(es) on port \(port).")
    }

    private func bundledPaths() -> (python: URL, server: URL)? {
        let env = ProcessInfo.processInfo.environment
        let resource = Bundle.main.resourceURL
        let python = env["AGENDA_EMBEDDED_PYTHON"].map(URL.init(fileURLWithPath:))
            ?? resource?.appendingPathComponent("python/bin/python3")
        let server = env["AGENDA_EMBEDDED_SERVER"].map(URL.init(fileURLWithPath:))
            ?? resource?.appendingPathComponent("server")
        guard let python, let server,
              FileManager.default.isExecutableFile(atPath: python.path),
              FileManager.default.fileExists(atPath: server.appendingPathComponent("main.py").path)
        else { return nil }
        return (python, server)
    }

    private func applicationSupportURL() throws -> URL {
        let base = try FileManager.default.url(
            for: .applicationSupportDirectory,
            in: .userDomainMask,
            appropriateFor: nil,
            create: true
        )
        let directory = base.appendingPathComponent(AgendaRuntime.dataFolderName, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        return directory
    }

    private func bundledAIHelperURL() -> URL? {
        if let override = ProcessInfo.processInfo.environment["AGENDA_LOCAL_AI_HELPER"] {
            let url = URL(fileURLWithPath: override)
            return FileManager.default.isExecutableFile(atPath: url.path) ? url : nil
        }
        guard #available(macOS 26.0, *),
              let resource = Bundle.main.resourceURL
        else { return nil }
        let helper = resource.appendingPathComponent("bin/AgendaAIHelper")
        return FileManager.default.isExecutableFile(atPath: helper.path) ? helper : nil
    }

    private func pidFileURL() -> URL? {
        try? applicationSupportURL().appendingPathComponent("server.pid")
    }

    private func writePIDFile(_ pid: Int32) {
        guard let url = pidFileURL() else { return }
        try? String(pid).write(to: url, atomically: true, encoding: .utf8)
    }

    private func removePIDFile() {
        guard let url = pidFileURL() else { return }
        try? FileManager.default.removeItem(at: url)
    }

    /// SIGTERM does not give SwiftUI's application delegate a chance to stop
    /// children. Reap only the PID recorded by a prior Agenda launch.
    private func terminateRecordedStaleProcess() async {
        guard let url = pidFileURL(),
              let raw = try? String(contentsOf: url, encoding: .utf8),
              let pid = Int32(raw.trimmingCharacters(in: .whitespacesAndNewlines)),
              Darwin.kill(pid, 0) == 0
        else {
            removePIDFile()
            return
        }
        sendSignal("TERM", to: pid)
        for _ in 0..<30 {
            if Darwin.kill(pid, 0) != 0 { break }
            try? await Task.sleep(nanoseconds: 100_000_000)
        }
        if Darwin.kill(pid, 0) == 0 {
            sendSignal("KILL", to: pid)
            for _ in 0..<10 {
                if Darwin.kill(pid, 0) != 0 { break }
                try? await Task.sleep(nanoseconds: 100_000_000)
            }
        }
        removePIDFile()
        appendLog("Stopped stale local server from the previous app launch.")
    }

    private func sendSignal(_ signal: String, to pid: Int32) {
        let command = Process()
        command.executableURL = URL(fileURLWithPath: "/bin/kill")
        command.arguments = ["-\(signal)", "\(pid)"]
        try? command.run()
        command.waitUntilExit()
    }

    private func prepareDatabase(pythonURL: URL, serverURL: URL) throws -> URL {
        let support = try applicationSupportURL()
        let database = support.appendingPathComponent("agenda.db")
        guard !FileManager.default.fileExists(atPath: database.path) else { return database }

        let seeder = Process()
        seeder.executableURL = pythonURL
        seeder.arguments = ["seed.py"]
        seeder.currentDirectoryURL = serverURL
        seeder.environment = serverEnvironment(databaseURL: database)
        try seeder.run()
        seeder.waitUntilExit()
        guard seeder.terminationStatus == 0 else {
            throw NSError(
                domain: "Agenda",
                code: 1,
                userInfo: [NSLocalizedDescriptionKey: "Could not seed the first-run Agenda database."]
            )
        }
        return database
    }

    private func serverEnvironment(databaseURL: URL) -> [String: String] {
        var env = ProcessInfo.processInfo.environment
        env["AGENDA_DB_PATH"] = databaseURL.path
        env["AGENDA_HOST"] = "127.0.0.1"
        env["AGENDA_PORT"] = "\(port)"
        env["AGENDA_READ_ONLY"] = "0"
        if let helper = bundledAIHelperURL() {
            env["AGENDA_LOCAL_AI_HELPER"] = helper.path
        } else {
            env.removeValue(forKey: "AGENDA_LOCAL_AI_HELPER")
        }
        env["PYTHONNOUSERSITE"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONUNBUFFERED"] = "1"
        env["PATH"] = "/usr/bin:/bin"
        return env
    }

    private func attach(_ pipe: Pipe, label: String, generation: Int) {
        pipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty, let text = String(data: data, encoding: .utf8) else { return }
            DispatchQueue.main.async {
                guard let self, generation == self.launchGeneration else { return }
                for line in text.split(whereSeparator: \.isNewline) {
                    self.appendLog("[\(label)] \(line)")
                    if line.localizedCaseInsensitiveContains("address already in use") {
                        self.status = .error("Port \(self.port) is already in use.")
                    }
                }
            }
        }
    }

    private func appendLog(_ line: String) {
        logLines.append(line)
        if logLines.count > 300 { logLines.removeFirst(logLines.count - 300) }
    }

    private func startHealthTimer() {
        stopHealthTimer()
        healthTimer = Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) { [weak self] _ in
            Task { await self?.healthCheck() }
        }
    }

    private func stopHealthTimer() {
        healthTimer?.invalidate()
        healthTimer = nil
    }

    private func healthCheck() async {
        guard process?.isRunning == true else { return }
        var request = URLRequest(url: localURL.appendingPathComponent("api/projects"))
        request.timeoutInterval = 1
        do {
            let (_, response) = try await URLSession.shared.data(for: request)
            if let response = response as? HTTPURLResponse, response.statusCode == 200 {
                status = .ready
                stopHealthTimer()
                appendLog("Local Agenda is ready on port \(port).")
            }
        } catch {
            // Uvicorn is still starting; the timer will retry.
        }
    }
}
