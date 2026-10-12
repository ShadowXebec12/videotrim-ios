import Foundation
import SwiftUI

typealias J = [String: Any]

extension Dictionary where Key == String, Value == Any {
    func s(_ k: String) -> String {
        if let v = self[k] as? String { return v }
        if let v = self[k] as? NSNumber { return v.stringValue }
        return ""
    }
    func i(_ k: String) -> Int? { (self[k] as? NSNumber)?.intValue }
    func d(_ k: String) -> Double? { (self[k] as? NSNumber)?.doubleValue }
    func b(_ k: String) -> Bool { (self[k] as? Bool) ?? false }
    func has(_ k: String) -> Bool { self[k] != nil && !(self[k] is NSNull) }
    func obj(_ k: String) -> J { (self[k] as? J) ?? [:] }
    func arr(_ k: String) -> [J] { (self[k] as? [J]) ?? [] }
    func strs(_ k: String) -> [String] { (self[k] as? [String]) ?? [] }
    func nums(_ k: String) -> [Double] { ((self[k] as? [NSNumber]) ?? []).map { $0.doubleValue } }
    var ok: Bool { b("ok") }
    /// The server's own words for a failure.
    var why: String {
        let e = s("error")
        return e.isEmpty ? s("message") : e
    }
}

enum API {
    static var server: String {
        var s = (UserDefaults.standard.string(forKey: "server") ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        while s.hasSuffix("/") { s.removeLast() }
        if !s.isEmpty && !s.contains("://") { s = "http://" + s }
        return s
    }
    static var key: String {
        (UserDefaults.standard.string(forKey: "key") ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private static let session: URLSession = {
        let c = URLSessionConfiguration.ephemeral
        c.timeoutIntervalForRequest = 40
        c.waitsForConnectivity = false
        return URLSession(configuration: c)
    }()

    /// Full address for a path. withKey puts the key in the address, for the video player, which cannot send headers.
    static func url(_ path: String, _ query: [String: String] = [:], withKey: Bool = false) -> URL? {
        guard !server.isEmpty, var parts = URLComponents(string: server + path) else { return nil }
        var all = query
        if withKey { all["key"] = key }
        if !all.isEmpty {
            parts.queryItems = all.sorted { $0.key < $1.key }.map { URLQueryItem(name: $0.key, value: $0.value) }
            // "+" and "&" inside a file name must not be read as separators
            parts.percentEncodedQuery = parts.percentEncodedQuery?.replacingOccurrences(of: "+", with: "%2B")
        }
        return parts.url
    }

    static func get(_ path: String, _ query: [String: String] = [:]) async -> J {
        await send("GET", path, query, nil)
    }

    static func post(_ path: String, _ query: [String: String] = [:], body: J? = nil) async -> J {
        await send("POST", path, query, body)
    }

    /// Raw bytes (one preview frame). nil on any failure.
    static func data(_ path: String, _ query: [String: String] = [:]) async -> Data? {
        guard let url = url(path, query) else { return nil }
        var request = URLRequest(url: url)
        request.setValue(key, forHTTPHeaderField: "X-Key")
        guard let (data, response) = try? await session.data(for: request),
              (response as? HTTPURLResponse)?.statusCode == 200 else { return nil }
        return data
    }

    private static func send(_ method: String, _ path: String, _ query: [String: String], _ body: J?) async -> J {
        guard !server.isEmpty else { return ["ok": false, "error": "Set the PC address in Settings"] }
        guard let url = url(path, query) else { return ["ok": false, "error": "Bad address"] }
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.setValue(key, forHTTPHeaderField: "X-Key")
        if let body = body {
            request.httpBody = try? JSONSerialization.data(withJSONObject: body)
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        do {
            let (data, response) = try await session.data(for: request)
            let status = (response as? HTTPURLResponse)?.statusCode ?? 0
            if var json = (try? JSONSerialization.jsonObject(with: data)) as? J {
                if status == 401 { json["error"] = "Wrong key. Check Settings." }
                if status >= 400 && json["ok"] == nil { json["ok"] = false }
                return json
            }
            return ["ok": false, "error": "Unexpected answer (HTTP \(status))"]
        } catch {
            return ["ok": false, "error": "Cannot reach the PC: \(error.localizedDescription)"]
        }
    }
}

/// Launch arguments used by the UI tests: -uiTestReset, -uiTestSet key=value
enum TestHooks {
    static func apply() {
        let args = ProcessInfo.processInfo.arguments
        if args.contains("-uiTestReset"), let id = Bundle.main.bundleIdentifier {
            UserDefaults.standard.removePersistentDomain(forName: id)
        }
        var index = 0
        while index < args.count - 1 {
            if args[index] == "-uiTestSet" {
                let pair = args[index + 1].split(separator: "=", maxSplits: 1).map(String.init)
                if pair.count == 2 { UserDefaults.standard.set(pair[1], forKey: pair[0]) }
            }
            index += 1
        }
    }
}

/// 65.5 -> "1:05.50", 3723 -> "1:02:03.00". The server reads this form back.
func stamp(_ seconds: Double) -> String {
    let s = max(0, seconds)
    let hours = Int(s) / 3600
    let minutes = Int(s) % 3600 / 60
    let rest = s - Double(hours * 3600 + minutes * 60)
    return hours > 0 ? String(format: "%d:%02d:%05.2f", hours, minutes, rest) : String(format: "%d:%05.2f", minutes, rest)
}

func fileSize(_ bytes: Int) -> String {
    let mb = Double(bytes) / 1_048_576
    return mb >= 1024 ? String(format: "%.2f GB", mb / 1024) : String(format: "%.1f MB", mb)
}

func shortDate(_ unix: Int) -> String {
    let f = DateFormatter()
    f.dateFormat = "yyyy-MM-dd HH:mm"
    return f.string(from: Date(timeIntervalSince1970: TimeInterval(unix)))
}

/// Red line shown at the top of a list when a request failed.
struct ErrorRow: View {
    let text: String
    var body: some View {
        if !text.isEmpty {
            Text(text).font(.footnote).foregroundColor(.red).accessibilityIdentifier("error")
        }
    }
}
