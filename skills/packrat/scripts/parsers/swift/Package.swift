// swift-tools-version: 6.0
import Foundation
import PackageDescription

#if os(Linux)
if !FileManager.default.fileExists(atPath: "/usr/libexec/swift/lib/swift/linux/lib_Testing_Foundation.so") {
    let pkgDir = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
    for arch in ["x86_64-pc-linux-gnu", "aarch64-unknown-linux-gnu"] {
        for cfg in ["debug", "release"] {
            let dir = pkgDir.appendingPathComponent(".build/\(arch)/\(cfg)", isDirectory: true)
            try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
            let stub = dir.appendingPathComponent("lib_Testing_Foundation.a")
            if !FileManager.default.fileExists(atPath: stub.path) {
                try? Data("!<arch>\n".utf8).write(to: stub)
            }
        }
    }
}
#endif

let package = Package(
    name: "PackratSwift",
    platforms: [
        .macOS(.v13)
    ],
    products: [
        .executable(
            name: "PackratSwift",
            targets: ["PackratSwift"]
        ),
    ],
    dependencies: [
        .package(url: "https://github.com/apple/swift-syntax.git", from: "600.0.0"),
    ],
    targets: [
        .executableTarget(
            name: "PackratSwift",
            dependencies: [
                .product(name: "SwiftSyntax", package: "swift-syntax"),
                .product(name: "SwiftParser", package: "swift-syntax"),
            ]
        ),
        .testTarget(
            name: "PackratSwiftTests",
            dependencies: [
                "PackratSwift",
                .product(name: "SwiftSyntax", package: "swift-syntax"),
                .product(name: "SwiftParser", package: "swift-syntax"),
            ]
        ),
    ]
)
