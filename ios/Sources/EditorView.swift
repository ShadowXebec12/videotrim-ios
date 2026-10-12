import AVKit
import SwiftUI

/// Everything about one video being edited. The PC does the cutting; this only holds the settings.
final class EditorModel: ObservableObject {
    var path = ""
    @Published var loaded = false
    @Published var error = ""
    @Published var info = ""
    @Published var duration = 0.0
    @Published var playable = false
    @Published var pos = 0.0
    @Published var frame: UIImage?
    @Published var resumed = false

    @Published var start = 0.0
    @Published var end = 10.0
    @Published var format = ""
    @Published var crf = 18
    @Published var rotation = 0
    @Published var height = 0
    @Published var fps = 0.0
    @Published var bitrate = ""
    @Published var prefix = "trim_"
    @Published var pose = ""
    @Published var addTag = true

    @Published var formats: [String] = []
    @Published var poses: [String] = []
    @Published var heights: [Int] = [0]
    @Published var fpsChoices: [Double] = [0]
    @Published var outDir = ""

    @Published var outName = ""
    @Published var status = ""
    @Published var percent = 0
    @Published var busy = false

    var player: AVPlayer?
    var dragging = false
    private var observer: Any?
    private var frameTask: Task<Void, Never>?
    private var jobId = ""

    /// Changes whenever a setting that affects the output file name changes.
    var nameKey: String {
        "\(loaded)|\(start)|\(end)|\(format)|\(prefix)|\(pose)|\(addTag)"
    }

    var settings: J {
        ["path": path, "start": start, "end": end, "format": format, "crf": crf, "rotation": rotation,
         "height": height, "fps": fps, "bitrate_k": Int(bitrate.trimmingCharacters(in: .whitespaces)) ?? 0,
         "prefix": prefix, "pose": pose, "add_tag": addTag]
    }

    @MainActor
    func load(_ path: String) async {
        if loaded { return }
        self.path = path
        let options = await API.get("/api/options")
        guard options.ok else {
            error = options.why
            return
        }
        formats = options.strs("formats")
        poses = options.strs("poses")
        heights = options.nums("heights").map { Int($0) }
        fpsChoices = options.nums("fps")
        outDir = options.s("out_dir")
        format = formats.first ?? ""
        prefix = options.s("prefix")
        crf = options.i("crf") ?? 18

        let answer = await API.get("/api/info", ["path": path])
        guard answer.ok else {
            error = answer.why
            return
        }
        duration = answer.d("duration") ?? 0
        playable = answer.b("playable")
        info = "\(answer.i("width") ?? 0)x\(answer.i("height") ?? 0), \(stamp(duration)) long, "
            + String(format: "%.4g fps, ", answer.d("fps") ?? 0) + fileSize(answer.i("size") ?? 0)
        end = min(10, duration)

        let edit = answer.obj("edit")
        if !edit.isEmpty {
            resumed = true
            start = edit.d("start") ?? start
            end = edit.d("end") ?? end
            if formats.contains(edit.s("format")) { format = edit.s("format") }
            crf = edit.i("crf") ?? crf
            rotation = edit.i("rotation") ?? 0
            height = edit.i("height") ?? 0
            fps = edit.d("fps") ?? 0
            if let rate = edit.i("bitrate_k"), rate > 0 { bitrate = String(rate) }
            if edit.has("prefix") { prefix = edit.s("prefix") }
            pose = edit.s("pose")
            if edit.has("add_tag") { addTag = edit.b("add_tag") }
        }
        if !heights.contains(height) { heights.append(height) }
        if !fpsChoices.contains(fps) { fpsChoices.append(fps) }
        if !pose.isEmpty && !poses.contains(pose) { poses.append(pose) }
        pos = min(start, duration)

        if playable, let url = API.url("/video", ["path": path], withKey: true) {
            let player = AVPlayer(url: url)
            self.player = player
            observer = player.addPeriodicTimeObserver(forInterval: CMTime(seconds: 0.1, preferredTimescale: 600),
                                                      queue: .main) { [weak self] time in
                guard let self = self, !self.dragging, time.seconds.isFinite else { return }
                self.pos = time.seconds
            }
            seek(pos)
        } else {
            await loadFrame(pos)
        }
        loaded = true
    }

    /// Move the preview to a time: the player if the phone can play this file, a still frame otherwise.
    func seek(_ time: Double) {
        let t = max(0, min(time, duration))
        pos = t
        if let player = player {
            player.pause()
            player.seek(to: CMTime(seconds: t, preferredTimescale: 600), toleranceBefore: .zero, toleranceAfter: .zero)
        } else {
            frameTask?.cancel()
            frameTask = Task { [weak self] in
                try? await Task.sleep(nanoseconds: 200_000_000)     // wait for the finger to settle
                if Task.isCancelled { return }
                await self?.loadFrame(t)
            }
        }
    }

    @MainActor
    func loadFrame(_ time: Double) async {
        if let data = await API.data("/frame", ["path": path, "t": String(format: "%.3f", time)]),
           let image = UIImage(data: data) {
            frame = image
        }
    }

    func nudge(start delta: Double) {
        start = max(0, min(start + delta, duration))
        seek(start)
    }

    func nudge(end delta: Double) {
        end = max(0, min(end + delta, duration))
        seek(end)
    }

    @MainActor
    func refreshName() async {
        guard loaded else { return }
        try? await Task.sleep(nanoseconds: 300_000_000)
        if Task.isCancelled { return }
        let answer = await API.post("/api/name", body: settings)
        if Task.isCancelled { return }
        outName = answer.ok ? answer.s("name") : answer.why
    }

    @MainActor
    func trim() async {
        busy = true
        percent = 0
        status = "Sending..."
        let answer = await API.post("/api/trim", body: settings)
        guard answer.ok else {
            status = "Failed: " + answer.why
            busy = false
            return
        }
        var job = answer.obj("job")
        jobId = job.s("id")
        while true {
            let state = job.s("status")
            percent = job.i("percent") ?? 0
            if state == "done" {
                status = "Done: " + job.s("message")
                break
            }
            if state == "failed" || state == "cancelled" {
                status = (state == "failed" ? "Failed: " : "") + job.s("message")
                break
            }
            status = state == "queued" ? "Waiting for another trim to finish..." : "Trimming \(percent)%"
            try? await Task.sleep(nanoseconds: 700_000_000)
            let next = await API.get("/api/job", ["id": jobId])
            guard next.ok else {
                status = "Failed: " + next.why
                break
            }
            job = next.obj("job")
        }
        busy = false
        await refreshName()
    }

    func cancel() {
        let id = jobId
        Task { _ = await API.post("/api/cancel", ["id": id]) }
    }

    /// Leaving the screen: stop playback and keep the settings on the PC for next time.
    func leave() {
        frameTask?.cancel()
        player?.pause()
        guard loaded else { return }
        let body = settings
        Task { _ = await API.post("/api/state", body: body) }
    }

    deinit {
        if let observer = observer { player?.removeTimeObserver(observer) }
    }
}

struct EditorView: View {
    let path: String
    let title: String
    @StateObject private var m = EditorModel()

    var body: some View {
        Form {
            if !m.error.isEmpty {
                ErrorRow(text: m.error)
            } else if !m.loaded {
                ProgressView("Reading video...")
            } else {
                preview
                marks
                output
                naming
                run
            }
        }
        .navigationTitle(title)
        .navigationBarTitleDisplayMode(.inline)
        .task { await m.load(path) }
        .task(id: m.nameKey) { await m.refreshName() }
        .onDisappear { m.leave() }
    }

    private var preview: some View {
        Section(footer: Text(m.info + (m.resumed ? "\nContinuing your last edit of this video." : ""))
            .accessibilityIdentifier("videoInfo")) {
            if let player = m.player {
                VideoPlayer(player: player).frame(height: 230)
            } else if let frame = m.frame {
                Image(uiImage: frame).resizable().scaledToFit().frame(maxWidth: .infinity).frame(height: 230)
                    .accessibilityIdentifier("framePreview")
            } else {
                Text("No preview for this file.").foregroundColor(.secondary)
            }
            Slider(value: Binding(get: { m.pos }, set: { m.seek($0) }), in: 0...max(m.duration, 0.1),
                   onEditingChanged: { m.dragging = $0 })
                .accessibilityIdentifier("scrub")
            HStack {
                Text(stamp(m.pos)).monospacedDigit().accessibilityIdentifier("position")
                Spacer()
                Button("Set Start") {
                    m.start = m.pos
                }.buttonStyle(.bordered).accessibilityIdentifier("setStart")
                Button("Set End") {
                    m.end = m.pos
                }.buttonStyle(.bordered).accessibilityIdentifier("setEnd")
            }
        }
    }

    private var marks: some View {
        Section(header: Text("Cut"), footer: Text(lengthNote).accessibilityIdentifier("lengthNote")) {
            markRow("Start", m.start, id: "start") { m.nudge(start: $0) }
            markRow("End", m.end, id: "end") { m.nudge(end: $0) }
        }
    }

    private var lengthNote: String {
        m.end > m.start ? String(format: "Length %.2f s", m.end - m.start) : "End must be after start."
    }

    private func markRow(_ label: String, _ value: Double, id: String, nudge: @escaping (Double) -> Void) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text(label)
                Spacer()
                Button(stamp(value)) { m.seek(value) }
                    .buttonStyle(.borderless).monospacedDigit().accessibilityIdentifier(id + "Value")
            }
            HStack(spacing: 6) {
                ForEach([-10.0, -1.0, -0.1, 0.1, 1.0, 10.0], id: \.self) { step in
                    Button((step > 0 ? "+" : "") + String(format: "%g", step)) { nudge(step) }
                        .buttonStyle(.bordered).font(.footnote)
                        .accessibilityIdentifier(id + (step > 0 ? "+" : "") + String(format: "%g", step))
                }
            }
        }
    }

    private var output: some View {
        Section(header: Text("Output"), footer: Text("Quality: lower is better and bigger. A bitrate replaces the quality value. Rotation is applied to the saved file, not this preview.")) {
            Picker("Format", selection: $m.format) {
                ForEach(m.formats, id: \.self) { Text($0).tag($0) }
            }.accessibilityIdentifier("formatPicker")
            Stepper("Quality \(m.crf)", value: $m.crf, in: 0...51).accessibilityIdentifier("quality")
            Picker("Resolution", selection: $m.height) {
                ForEach(m.heights, id: \.self) { Text($0 == 0 ? "Same as source" : "\($0)p").tag($0) }
            }.accessibilityIdentifier("heightPicker")
            Picker("Frame rate", selection: $m.fps) {
                ForEach(m.fpsChoices, id: \.self) { Text($0 == 0 ? "Same as source" : String(format: "%g fps", $0)).tag($0) }
            }.accessibilityIdentifier("fpsPicker")
            HStack {
                Text("Bitrate kbit/s")
                TextField("auto", text: $m.bitrate).keyboardType(.numberPad).multilineTextAlignment(.trailing)
                    .accessibilityIdentifier("bitrate")
            }
            Picker("Rotate", selection: $m.rotation) {
                ForEach([0, 90, 180, 270], id: \.self) { Text($0 == 0 ? "None" : "\($0) degrees").tag($0) }
            }.accessibilityIdentifier("rotationPicker")
        }
    }

    private var naming: some View {
        Section(header: Text("File name"), footer: Text("Saves to " + m.outDir + "\n" + m.outName)
            .accessibilityIdentifier("outName")) {
            HStack {
                Text("Prefix")
                TextField("none", text: $m.prefix).textInputAutocapitalization(.never).autocorrectionDisabled()
                    .multilineTextAlignment(.trailing).accessibilityIdentifier("prefix")
            }
            Picker("Pose name", selection: $m.pose) {
                Text("None").tag("")
                ForEach(m.poses, id: \.self) { Text($0).tag($0) }
            }.accessibilityIdentifier("posePicker")
            Toggle("Add start time to name", isOn: $m.addTag).accessibilityIdentifier("addTag")
        }
    }

    private var run: some View {
        Section {
            Button(m.busy ? "Working..." : "Trim") { Task { await m.trim() } }
                .disabled(m.busy || m.end <= m.start)
                .accessibilityIdentifier("trimButton")
            if m.busy {
                ProgressView(value: Double(m.percent), total: 100)
                Button("Cancel", role: .destructive) { m.cancel() }.accessibilityIdentifier("cancelButton")
            }
            if !m.status.isEmpty {
                Text(m.status).font(.footnote).accessibilityIdentifier("status")
            }
        }
    }
}
