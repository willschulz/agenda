// swift-tools-version:6.0
import PackageDescription

let package = Package(
    name: "Agenda",
    platforms: [.macOS(.v13)],
    dependencies: [
        .package(path: "../../apple/AgendaSyncCore"),
        .package(
            url: "https://github.com/sparkle-project/Sparkle",
            exact: "2.9.5"
        ),
    ],
    targets: [
        .target(
            name: "AgendaLocalAI",
            path: "Sources/AgendaLocalAI"
        ),
        .executableTarget(
            name: "AgendaAIHelper",
            dependencies: ["AgendaLocalAI"],
            path: "Sources/AgendaAIHelper"
        ),
        .executableTarget(
            name: "Agenda",
            dependencies: [
                .product(name: "AgendaSyncCore", package: "AgendaSyncCore"),
                .product(name: "Sparkle", package: "Sparkle"),
            ],
            path: "Sources/Agenda",
            linkerSettings: [
                // SwiftPM's command-line build only adds @loader_path, while
                // the assembled app follows the standard macOS layout with
                // third-party frameworks in Contents/Frameworks.
                .unsafeFlags([
                    "-Xlinker", "-rpath",
                    "-Xlinker", "@executable_path/../Frameworks",
                ]),
            ]
        ),
        .testTarget(
            name: "AgendaLocalAITests",
            dependencies: ["AgendaLocalAI"],
            path: "Tests/AgendaLocalAITests"
        ),
        .testTarget(
            name: "AgendaTests",
            dependencies: [
                "Agenda",
                .product(name: "AgendaSyncCore", package: "AgendaSyncCore"),
            ],
            path: "Tests/AgendaTests"
        ),
    ],
    // Swift 5 language mode to avoid strict-Sendable friction with
    // WKWebView delegate callbacks.
    swiftLanguageModes: [.v5]
)
