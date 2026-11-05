// swift-tools-version:6.0
import PackageDescription

let package = Package(
    name: "AgendaSyncCore",
    platforms: [
        .macOS(.v13),
        .iOS(.v17),
    ],
    products: [
        .library(name: "AgendaSyncCore", targets: ["AgendaSyncCore"]),
    ],
    targets: [
        .target(name: "AgendaSyncCore"),
        .testTarget(
            name: "AgendaSyncCoreTests",
            dependencies: ["AgendaSyncCore"],
            resources: [.process("Fixtures")]
        ),
    ],
    swiftLanguageModes: [.v5]
)

