// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "CognitiveComplexity",
    platforms: [
        .macOS(.v13)
    ],
    products: [
        .executable(
            name: "CognitiveComplexity",
            targets: ["CognitiveComplexity"]
        ),
    ],
    dependencies: [
        .package(url: "https://github.com/apple/swift-syntax.git", from: "600.0.0"),
    ],
    targets: [
        .executableTarget(
            name: "CognitiveComplexity",
            dependencies: [
                .product(name: "SwiftSyntax", package: "swift-syntax"),
                .product(name: "SwiftParser", package: "swift-syntax"),
            ]
        ),
        .testTarget(
            name: "CognitiveComplexityTests",
            dependencies: [
                "CognitiveComplexity",
                .product(name: "SwiftSyntax", package: "swift-syntax"),
                .product(name: "SwiftParser", package: "swift-syntax"),
            ]
        ),
    ]
)
