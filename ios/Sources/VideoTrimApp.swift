import SwiftUI

@main
struct VideoTrimApp: App {
    init() { TestHooks.apply() }

    var body: some Scene {
        WindowGroup {
            RootView()
        }
    }
}

struct RootView: View {
    @AppStorage("server") private var server = ""
    @State private var tab = 0

    var body: some View {
        TabView(selection: $tab) {
            BrowseTab()
                .tabItem { Label("Browse", systemImage: "folder") }.tag(0)
            RecentTab()
                .tabItem { Label("Recent", systemImage: "clock.arrow.circlepath") }.tag(1)
            SettingsView()
                .tabItem { Label("Settings", systemImage: "gearshape") }.tag(2)
        }
        .onAppear { if server.isEmpty { tab = 2 } }
    }
}

struct SettingsView: View {
    @AppStorage("server") private var server = ""
    @AppStorage("key") private var key = ""
    @State private var result = ""
    @State private var testing = false

    var body: some View {
        NavigationStack {
            Form {
                Section(header: Text("PC"), footer: Text("Address of the Video Trim server on your PC, for example 192.168.1.10:8776. The key is in config.json next to trim_server.py.")) {
                    TextField("192.168.1.10:8776", text: $server)
                        .keyboardType(.URL).textInputAutocapitalization(.never).autocorrectionDisabled()
                        .accessibilityIdentifier("serverField")
                    SecureField("Key", text: $key)
                        .textContentType(.oneTimeCode)      // stops the iOS "Save Password?" prompt
                        .textInputAutocapitalization(.never).autocorrectionDisabled()
                        .accessibilityIdentifier("keyField")
                }
                Section {
                    Button(testing ? "Testing..." : "Test connection") { Task { await test() } }
                        .disabled(testing)
                        .accessibilityIdentifier("testConnection")
                    if !result.isEmpty {
                        Text(result).font(.footnote).accessibilityIdentifier("testResult")
                    }
                }
            }
            .navigationTitle("Settings")
        }
    }

    @MainActor
    private func test() async {
        testing = true
        let ping = await API.get("/api/ping")
        if !ping.ok {
            result = ping.why
        } else {
            let top = await API.get("/api/browse")
            result = top.ok ? "Connected. \(top.arr("folders").count) folders. Saves to \(top.s("out_dir"))" : top.why
        }
        testing = false
    }
}
