import SwiftUI

struct RootView: View {
    @EnvironmentObject var state: AppState
    /// Launch with `-selectedTab 1` / `-mock YES` to jump straight to a screen (used for screenshots).
    @AppStorage("selectedTab") private var tab = 0

    var body: some View {
        TabView(selection: $tab) {
            CaptureView().tabItem { Label("Capture", systemImage: "video.fill") }.tag(0)
            ResultsView().tabItem { Label("Results", systemImage: "checklist") }.tag(1)
                .badge(state.cards.filter { $0.verdict == .conflict }.count)
            AskView().tabItem { Label("Ask", systemImage: "questionmark.bubble") }.tag(3)
            SettingsView().tabItem { Label("Settings", systemImage: "gearshape.fill") }.tag(2)
        }
        .onAppear {
            let a = UITabBarAppearance()
            a.configureWithOpaqueBackground()
            a.backgroundColor = UIColor(Theme.paper)
            UITabBar.appearance().standardAppearance = a
            UITabBar.appearance().scrollEdgeAppearance = a
            let n = UINavigationBarAppearance()
            n.configureWithOpaqueBackground()
            n.backgroundColor = UIColor(Theme.paper)
            n.titleTextAttributes = [.foregroundColor: UIColor(Theme.ink)]
            n.largeTitleTextAttributes = [.foregroundColor: UIColor(Theme.ink)]
            UINavigationBar.appearance().standardAppearance = n
            UINavigationBar.appearance().scrollEdgeAppearance = n
            state.loadMockIfNeeded()
        }
    }
}

// MARK: - Settings

struct SettingsView: View {
    @EnvironmentObject var state: AppState
    @AppStorage("baseURL") private var baseURL = AppState.defaultBase
    @AppStorage("sheet") private var sheet = ""
    @AppStorage("mock") private var mock = false
    @State private var status: String?
    @State private var statusOK = false
    @State private var testing = false
    private let sheets = ["", "P101", "A201", "E200", "S101", "M301"]

    var body: some View {
        NavigationStack {
            Form {
                Section("Backend") {
                    TextField("http://127.0.0.1:8000 (simulator) or http://<mac-lan-ip>:8000", text: $baseURL)
                        .textInputAutocapitalization(.never).autocorrectionDisabled()
                        .keyboardType(.URL)
                    Button {
                        Task {
                            testing = true; status = nil
                            do {
                                let h = try await state.api.health()
                                statusOK = h.ok
                                status = "OK  cache: \(h.cache_mode ?? "?")  spend: \(h.run_spend.map { String(format: "$%.2f", $0) } ?? "?")"
                            } catch {
                                statusOK = false
                                status = error.localizedDescription
                            }
                            testing = false
                        }
                    } label: {
                        HStack { Text("Test connection"); Spacer(); if testing { ProgressView() } }
                    }
                    if let status {
                        Label(status, systemImage: statusOK ? "checkmark.circle.fill" : "xmark.octagon.fill")
                            .foregroundStyle(statusOK ? Theme.green : Theme.red)
                            .font(.footnote)
                    }
                }
                Section {
                    TextField("Sheet hint, e.g. P101 (optional)", text: $sheet)
                        .textInputAutocapitalization(.characters).autocorrectionDisabled()
                    Picker("Quick pick", selection: $sheet) {
                        ForEach(sheets, id: \.self) { Text($0.isEmpty ? "None" : $0).tag($0) }
                    }
                } header: { Text("Sheet hint") } footer: { Text("Sent as ?sheet= with video uploads to narrow the search.") }
                Section {
                    Toggle("Mock responses (demo)", isOn: $mock)
                } footer: { Text("Skips the network and loads 3 bundled sample cards.") }
            }
            .scrollContentBackground(.hidden)
            .background(BlueprintGrid().ignoresSafeArea())
            .navigationTitle("Settings")
        }
    }
}
