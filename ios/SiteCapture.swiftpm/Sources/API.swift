import Foundation

enum APIError: LocalizedError {
    case badURL
    case http(Int, String)
    case tooLarge(Int)
    case sampleOnly
    var errorDescription: String? {
        switch self {
        case .sampleOnly: return "Sample mode only shows recorded results. Turn it off in Settings to run a live check."
        case .badURL: return "Backend URL is not valid. Check Settings."
        case .http(let code, let body): return "Backend returned \(code). \(body.prefix(200))"
        case .tooLarge(let mb): return "Clip is \(mb) MB; the backend limit is 50 MB."
        }
    }
}

/// Talks only to our backend. No third-party keys live in the app.
final class API: NSObject, URLSessionTaskDelegate {
    let base: String
    let mock: Bool
    private var progress: ((Double) -> Void)?

    init(base: String, mock: Bool) {
        self.base = base.trimmingCharacters(in: .whitespacesAndNewlines).trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        self.mock = mock
    }

    func url(_ path: String, query: [URLQueryItem] = []) throws -> URL {
        guard var comps = URLComponents(string: base + path), comps.host != nil else { throw APIError.badURL }
        if !query.isEmpty { comps.queryItems = query }
        guard let u = comps.url else { throw APIError.badURL }
        return u
    }

    /// Backend-relative asset path ("/crops/x.png", "crops/x.png", "/frames/y.jpg") -> absolute URL.
    func cropURL(_ crop: String) -> URL? { try? url(crop.hasPrefix("/") ? crop : "/" + crop) }

    /// Deep link to the card in the web UI.
    func webURL(cardID: String) -> URL? {
        guard let enc = cardID.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) else { return nil }
        return URL(string: base + "/#card=" + enc)
    }

    func ask(_ text: String) async throws -> Card {
        if mock { throw APIError.sampleOnly }
        var req = URLRequest(url: try url("/api/ask"))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONEncoder().encode(["text": text])
        req.timeoutInterval = 180
        let (d, r) = try await URLSession.shared.data(for: req)
        try check(d, r)
        return try JSONDecoder().decode(Card.self, from: d)
    }

    private func check(_ data: Data, _ resp: URLResponse) throws {
        if let h = resp as? HTTPURLResponse, !(200..<300).contains(h.statusCode) {
            throw APIError.http(h.statusCode, String(decoding: data, as: UTF8.self))
        }
    }

    func health() async throws -> Health {
        if mock { return Health(mock: true) }
        var req = URLRequest(url: try url("/api/health"))
        req.timeoutInterval = 6
        let (d, r) = try await URLSession.shared.data(for: req)
        try check(d, r)
        return try JSONDecoder().decode(Health.self, from: d)
    }

    func checkClaim(_ claim: String) async throws -> Card {
        if mock { throw APIError.sampleOnly }
        var req = URLRequest(url: try url("/api/check"))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONEncoder().encode(["claim": claim])
        req.timeoutInterval = 120
        let (d, r) = try await URLSession.shared.data(for: req)
        try check(d, r)
        return try JSONDecoder().decode(Card.self, from: d)
    }

    func uploadVideo(_ file: URL, sheet: String, onProgress: @escaping (Double) -> Void) async throws -> VideoResponse {
        if mock { throw APIError.sampleOnly }
        let size = (try? file.resourceValues(forKeys: [.fileSizeKey]).fileSize) ?? 0
        if size > 50 * 1_000_000 { throw APIError.tooLarge(size / 1_000_000) }
        let q = sheet.isEmpty ? [] : [URLQueryItem(name: "sheet", value: sheet)]
        var req = URLRequest(url: try url("/api/video", query: q))
        req.httpMethod = "POST"
        // Backend accepts only video/mp4 and video/quicktime; a picked file may be either.
        req.setValue(file.pathExtension.lowercased() == "mp4" ? "video/mp4" : "video/quicktime", forHTTPHeaderField: "Content-Type")
        req.timeoutInterval = 300
        progress = onProgress
        let session = URLSession(configuration: .default, delegate: self, delegateQueue: nil)
        defer { session.finishTasksAndInvalidate() }
        let (d, r) = try await session.upload(for: req, fromFile: file)
        try check(d, r)
        return try JSONDecoder().decode(VideoResponse.self, from: d)
    }

    func urlSession(_ s: URLSession, task: URLSessionTask, didSendBodyData _: Int64, totalBytesSent sent: Int64, totalBytesExpectedToSend total: Int64) {
        guard total > 0 else { return }
        progress?(Double(sent) / Double(total))
    }

}

extension Health {
    init(mock: Bool) { ok = true; cache_mode = "mock"; run_spend = 0 }
}
