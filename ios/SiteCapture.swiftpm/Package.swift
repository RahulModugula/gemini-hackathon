// swift-tools-version: 5.9
import PackageDescription
import AppleProductTypes

let package = Package(
    name: "SiteCapture",
    platforms: [.iOS("17.0")],
    products: [
        .iOSApplication(
            name: "SiteCapture",
            targets: ["AppModule"],
            bundleIdentifier: "com.example.SiteCapture",
            teamIdentifier: "",
            displayVersion: "1.0",
            bundleVersion: "1",
            appIcon: .placeholder(icon: .map),
            accentColor: .presetColor(.blue),
            supportedDeviceFamilies: [.phone],
            supportedInterfaceOrientations: [.portrait],
            additionalInfoPlistContentFilePath: "Info.plist"
        )
    ],
    targets: [
        .executableTarget(
            name: "AppModule",
            path: "Sources"
        )
    ]
)
