import SwiftUI

struct Entry: Identifiable, Hashable {
    let name: String
    let path: String
    var size = 0
    var mtime = 0
    var edited = false
    var id: String { path }
}

struct BrowseTab: View {
    var body: some View {
        NavigationStack {
            FolderView(path: "", title: "Videos")
        }
    }
}

/// One folder on the PC. An empty path is the list of allowed top folders.
struct FolderView: View {
    let path: String
    let title: String
    @State private var folders: [Entry] = []
    @State private var files: [Entry] = []
    @State private var error = ""
    @State private var loaded = false
    @State private var cut = false
    @State private var search = ""
    @AppStorage("sortByDate") private var byDate = false
    @AppStorage("server") private var server = ""
    @AppStorage("key") private var key = ""

    private var shownFolders: [Entry] {
        search.isEmpty ? folders : folders.filter { $0.name.localizedCaseInsensitiveContains(search) }
    }
    private var shownFiles: [Entry] {
        let rows = search.isEmpty ? files : files.filter { $0.name.localizedCaseInsensitiveContains(search) }
        return byDate ? rows.sorted { $0.mtime > $1.mtime } : rows
    }

    var body: some View {
        List {
            ErrorRow(text: error)
            if loaded && error.isEmpty && folders.isEmpty && files.isEmpty {
                Text("No videos or folders here.").foregroundColor(.secondary).accessibilityIdentifier("empty")
            }
            if cut {
                Text("This folder is very large. Only the first part is shown.").font(.footnote).foregroundColor(.orange)
            }
            ForEach(shownFolders) { folder in
                NavigationLink(destination: FolderView(path: folder.path, title: folder.name)) {
                    Label(folder.name, systemImage: "folder")
                }
                .accessibilityIdentifier("folder-" + folder.name)
            }
            ForEach(shownFiles) { file in
                NavigationLink(destination: EditorView(path: file.path, title: file.name)) {
                    VStack(alignment: .leading, spacing: 2) {
                        Label(file.name, systemImage: file.edited ? "scissors" : "film")
                        Text(fileSize(file.size) + "  " + shortDate(file.mtime))
                            .font(.caption).foregroundColor(.secondary)
                    }
                }
                .accessibilityIdentifier("file-" + file.name)
            }
        }
        .navigationTitle(title)
        .navigationBarTitleDisplayMode(.inline)
        .searchable(text: $search, prompt: "Filter this folder")
        .toolbar {
            ToolbarItem(placement: .navigationBarTrailing) {
                Button(byDate ? "Newest" : "A-Z") { byDate.toggle() }.accessibilityIdentifier("sortButton")
            }
        }
        .refreshable { await load() }
        // also reloads after the address or key is changed in Settings
        .task(id: server + "|" + key) { await load() }
    }

    @MainActor
    private func load() async {
        let answer = await API.get("/api/browse", path.isEmpty ? [:] : ["path": path])
        loaded = true
        guard answer.ok else {
            error = answer.why
            return
        }
        error = ""
        cut = answer.b("cut")
        folders = answer.arr("folders").map { Entry(name: $0.s("name"), path: $0.s("path")) }
        files = answer.arr("files").map {
            Entry(name: $0.s("name"), path: $0.s("path"), size: $0.i("size") ?? 0, mtime: $0.i("mtime") ?? 0,
                  edited: $0.b("edited"))
        }
    }
}

/// Videos with saved settings, newest first. Opening one continues where it was left.
struct RecentTab: View {
    @State private var rows: [J] = []
    @State private var error = ""
    @State private var loaded = false

    var body: some View {
        NavigationStack {
            List {
                ErrorRow(text: error)
                if loaded && error.isEmpty && rows.isEmpty {
                    Text("Nothing edited yet.").foregroundColor(.secondary).accessibilityIdentifier("empty")
                }
                ForEach(Array(rows.enumerated()), id: \.offset) { _, row in
                    NavigationLink(destination: EditorView(path: row.s("path"), title: row.s("name"))) {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(row.s("name"))
                            Text(stamp(row.d("start") ?? 0) + " to " + stamp(row.d("end") ?? 0) + "   " + shortDate(row.i("updated") ?? 0))
                                .font(.caption).foregroundColor(.secondary)
                        }
                    }
                    .accessibilityIdentifier("recent-" + row.s("name"))
                }
            }
            .navigationTitle("Recent edits")
            .refreshable { await load() }
            .task { await load() }
            .onAppear { Task { await load() } }
        }
    }

    @MainActor
    private func load() async {
        let answer = await API.get("/api/recent")
        loaded = true
        error = answer.ok ? "" : answer.why
        if answer.ok { rows = answer.arr("edits") }
    }
}
