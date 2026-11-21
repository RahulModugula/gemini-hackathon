import Foundation

enum APIError: LocalizedError {
    case badURL
    case http(Int, String)
    case tooLarge(Int)
    var errorDescription: String? {
        switch self {
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

    func cropURL(_ crop: String) -> URL? { try? url(crop.hasPrefix("/") ? crop : "/" + crop) }

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
        if mock { return Self.sample().cards[0] }
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
        if mock {
            for i in 1...5 { try await Task.sleep(nanoseconds: 120_000_000); onProgress(Double(i) / 5) }
            return Self.sample()
        }
        let size = (try? file.resourceValues(forKeys: [.fileSizeKey]).fileSize) ?? 0
        if size > 50 * 1_000_000 { throw APIError.tooLarge(size / 1_000_000) }
        let q = sheet.isEmpty ? [] : [URLQueryItem(name: "sheet", value: sheet)]
        var req = URLRequest(url: try url("/api/video", query: q))
        req.httpMethod = "POST"
        req.setValue("video/quicktime", forHTTPHeaderField: "Content-Type")
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

    static func sample() -> VideoResponse {
        guard let v = try? JSONDecoder().decode(VideoResponse.self, from: Data(SampleData.json.utf8))
        else { return VideoResponse(claims: nil, cards: [], video_s: nil) }
        return v
    }
}

extension Health {
    init(mock: Bool) { ok = true; cache_mode = "mock"; run_spend = 0 }
}
