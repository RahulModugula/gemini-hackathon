import Foundation

enum Verdict: String, Codable {
    case conflict, consistent, unclear
    init(from decoder: Decoder) throws {
        let s = try decoder.singleValueContainer().decode(String.self)
        self = Verdict(rawValue: s.lowercased()) ?? .unclear
    }
}

struct Claim: Codable, Hashable {
    var claim: String
    var kind: String?
    var t_start_s: Double?
    var t_end_s: Double?
    var quote_or_observation: String?
}

struct Evidence: Codable, Hashable {
    var sheet: String?
    var revision: String?
    var crop: String?
    /// [ymin, xmin, ymax, xmax], normalised 0-1000 on the crop image.
    var box_2d_on_crop: [Double]?
    var label: String?
}

struct Check: Codable, Hashable {
    var by: String?
    var check: String?
    var pass: Bool?
    var verdict: String?
    var note: String?

    /// true = tick, false = cross, nil = neutral.
    var passed: Bool? {
        if let pass { return pass }
        switch verdict?.lowercased() {
        case "consistent": return true
        case "conflict": return false
        default: return nil
        }
    }
}

struct Card: Codable, Hashable, Identifiable {
    var id: String
    var claim: String
    var verdict: Verdict
    var confidence: Double?
    var reason: String?
    var evidence: [Evidence]?
    var checks: [Check]?
    /// Not in the backend contract; filled client-side from the matching claim.
    var t_start_s: Double?
}

struct VideoResponse: Codable {
    var claims: [Claim]?
    var cards: [Card]
    var video_s: Double?

    /// Attach clip timestamps from `claims` to cards by claim text, then by index.
    func withTimestamps() -> [Card] {
        cards.enumerated().map { i, card in
            var c = card
            if c.t_start_s == nil {
                let match = claims?.first { $0.claim == card.claim } ?? (claims.flatMap { i < $0.count ? $0[i] : nil })
                c.t_start_s = match?.t_start_s
            }
            return c
        }
    }
}

struct Health: Codable {
    var ok: Bool
    var cache_mode: String?
    var run_spend: Double?

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = try c.decode(Bool.self, forKey: .ok)
        cache_mode = try? c.decode(String.self, forKey: .cache_mode)
        run_spend = try? c.decode(Double.self, forKey: .run_spend)
    }
    enum CodingKeys: String, CodingKey { case ok, cache_mode, run_spend }
}
