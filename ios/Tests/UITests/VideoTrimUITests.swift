import XCTest

/// Runs the real app in the simulator against Tests/mock_server.py (same answer shapes as the PC server).
/// Each test only reads state it changed itself, because the mock keeps its state for the whole run.
final class VideoTrimUITests: XCTestCase {
    var app: XCUIApplication!

    override func setUp() {
        continueAfterFailure = false
    }

    // MARK: helpers

    private func launch(server: String? = "http://127.0.0.1:8776", key: String = "test") {
        app = XCUIApplication()
        var args = ["-uiTestReset"]
        if let server = server { args += ["-uiTestSet", "server=" + server, "-uiTestSet", "key=" + key] }
        app.launchArguments = args
        app.launch()
    }

    private func el(_ id: String) -> XCUIElement {
        app.descendants(matching: .any).matching(identifier: id).firstMatch
    }

    private func shot(_ name: String) {
        let a = XCTAttachment(screenshot: app.screenshot())
        a.name = name
        a.lifetime = .keepAlways
        add(a)
    }

    /// Lists are lazy: a row far down does not exist until it is scrolled to.
    private func need(_ e: XCUIElement, _ what: String, swipes: Int = 8, file: StaticString = #filePath, line: UInt = #line) {
        if e.waitForExistence(timeout: 12) { return }
        for _ in 0..<swipes {
            app.swipeUp()
            if e.waitForExistence(timeout: 1.5) { return }
        }
        shot("MISSING " + what)
        XCTFail("missing: " + what, file: file, line: line)
    }

    private func waitFor(_ what: String, timeout: TimeInterval = 20, file: StaticString = #filePath, line: UInt = #line,
                         _ test: () -> Bool) {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if test() { return }
            usleep(250_000)
        }
        shot("WAIT " + what)
        XCTFail("never true: " + what, file: file, line: line)
    }

    private func tab(_ name: String) {
        let button = app.tabBars.buttons[name]
        XCTAssertTrue(button.waitForExistence(timeout: 15), "tab " + name)
        button.tap()
    }

    private func open(_ file: String) {
        need(el("folder-Videos"), "top folder")
        el("folder-Videos").tap()
        need(el("folder-Clips"), "Clips folder")
        el("folder-Clips").tap()
        need(el("file-" + file), "file " + file)
        el("file-" + file).tap()
        need(el("trimButton"), "editor for " + file)
    }

    // MARK: tests

    func test01FirstRunSettings() {
        launch(server: nil)
        let field = el("serverField")
        need(field, "settings tab on first run")
        field.tap()
        field.typeText("127.0.0.1:8776")
        let key = el("keyField")
        key.tap()
        key.typeText("test")
        el("testConnection").tap()
        let result = el("testResult")
        waitFor("connected message") { result.exists && result.label.contains("Connected. 1 folders") }
        shot("settings")
        tab("Browse")
        need(el("folder-Videos"), "top folder after setup")
    }

    func test02WrongKeyShowsWhy() {
        launch(key: "nope")
        let error = el("error")
        waitFor("wrong key message") { error.exists && error.label.contains("Wrong key") }
    }

    func test03BrowseFolders() {
        launch()
        need(el("folder-Videos"), "top folder")
        el("folder-Videos").tap()
        need(el("folder-Clips"), "Clips folder")
        need(el("folder-Empty"), "Empty folder")
        el("folder-Empty").tap()
        need(el("empty"), "empty folder note")
        app.navigationBars.buttons.element(boundBy: 0).tap()
        el("folder-Clips").tap()
        need(el("file-alpha one.mp4"), "mp4 row")
        need(el("file-beta.mkv"), "mkv row")
        shot("clips")
    }

    func test04TrimPlayableVideo() {
        launch()
        open("alpha one.mp4")
        let info = el("videoInfo")
        waitFor("video info") { info.exists && info.label.contains("64x64") }
        let name = el("outName")
        need(name, "output name")
        waitFor("default output name") { name.label.contains("trim_alpha one_0000.mp4") }
        shot("editor mp4")
        need(el("trimButton"), "trim button")
        el("trimButton").tap()
        let status = el("status")
        need(status, "status line")
        waitFor("done message") { status.label.contains("Done: trim_alpha one_0000.mp4") }
        shot("trim done")
    }

    func test05FramePreviewAndNudge() {
        launch()
        open("beta.mkv")
        app.swipeDown()
        need(el("framePreview"), "still frame preview for mkv", swipes: 0)
        need(el("start+1"), "start nudge")
        el("start+1").tap()
        el("start+1").tap()
        let name = el("outName")
        need(name, "output name")
        waitFor("name follows the new start") { name.label.contains("trim_beta_0002.mp4") }
        shot("editor mkv")
    }

    func test06RecentResumes() {
        launch()
        open("beta.mkv")
        need(el("start+10"), "start nudge")
        el("start+10").tap()
        need(el("trimButton"), "trim button")
        el("trimButton").tap()
        let status = el("status")
        need(status, "status line")
        waitFor("done message") { status.label.contains("Done:") }
        tab("Recent")
        let row = el("recent-beta.mkv")
        need(row, "recent row")
        row.tap()
        let info = el("videoInfo")
        waitFor("resume note") { info.exists && info.label.contains("Continuing your last edit") }
        shot("resumed")
    }
}
