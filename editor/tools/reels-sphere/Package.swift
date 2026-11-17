// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "reels-sphere", platforms: [.macOS(.v14)],
    products: [.executable(name: "reels-sphere", targets: ["ReelsSphere"])],
    targets: [
        .executableTarget(name: "ReelsSphere", exclude: ["Sphere.metal"]),
        .testTarget(name: "ReelsSphereTests", dependencies: ["ReelsSphere"]),
    ], swiftLanguageModes: [.v5]
)
