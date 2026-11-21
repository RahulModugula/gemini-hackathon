import SwiftUI

@main
struct SiteCaptureApp: App {
    @StateObject private var state = AppState()
    var body: some Scene {
        WindowGroup {
            RootView()
                .environmentObject(state)
                .preferredColorScheme(.light)
                .tint(Theme.blue)
        }
    }
}

@MainActor
final class AppState: ObservableObject {
    @Published var cards: [Card] = []
    @Published var videoSeconds: Double?
    @Published var busy = false
    @Published var uploadProgress: Double = 0
    @Published var error: String?

    // Edited in SettingsView via @AppStorage; read from UserDefaults here.
    /// Simulator reaches the Mac at 127.0.0.1. On a real phone set the Mac's LAN IP in Settings.
    static let defaultBase = "http://127.0.0.1:8000"
    @Published var asked: [Card] = []
    var baseURL: String { UserDefaults.standard.string(forKey: "baseURL") ?? Self.defaultBase }
    var sheet: String { (UserDefaults.standard.string(forKey: "sheet") ?? "").trimmingCharacters(in: .whitespaces) }
    var mock: Bool { UserDefaults.standard.bool(forKey: "mock") }

    var api: API { API(base: baseURL, mock: mock) }

    func upload(_ file: URL) async -> Bool {
        busy = true; error = nil; uploadProgress = 0
        defer { busy = false }
        do {
            let r = try await api.uploadVideo(file, sheet: sheet) { [weak self] p in
                Task { @MainActor in self?.uploadProgress = p }
            }
            cards = r.withTimestamps() + cards.filter { c in !r.cards.contains { $0.id == c.id } }
            videoSeconds = r.video_s
            return true
        } catch {
            self.error = error.localizedDescription
            return false
        }
    }

    func checkClaim(_ text: String) async -> Bool {
        busy = true; error = nil
        defer { busy = false }
        do {
            let c = try await api.checkClaim(text)
            cards.insert(c, at: 0)
            return true
        } catch {
            self.error = error.localizedDescription
            return false
        }
    }

    func ask(_ text: String) async -> Bool {
        busy = true; error = nil
        defer { busy = false }
        do {
            asked.insert(try await api.ask(text), at: 0)
            return true
        } catch {
            self.error = error.localizedDescription
            return false
        }
    }

    func loadMockIfNeeded() {
        if mock && cards.isEmpty { cards = API.sample().withTimestamps() }
    }
}
