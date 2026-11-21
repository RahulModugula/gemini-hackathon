import SwiftUI

/// Recorded results from our real drawing set (copied unchanged from ui/cards.json).
/// Shown only in the clearly labelled "Sample results (recorded)" section when there are no live results.
enum SampleData {
    static let cards: [Card] = {
        guard let url = Bundle.main.url(forResource: "sample_cards", withExtension: "json", subdirectory: "Resources"),
              let data = try? Data(contentsOf: url),
              let cards = try? JSONDecoder().decode([Card].self, from: data) else { return [] }
        return cards
    }()

    static func isSample(_ card: Card) -> Bool { cards.contains { $0.id == card.id } }

    /// Bundled crop image for a recorded card's evidence ("crops/x.png").
    static func crop(_ path: String) -> UIImage? {
        let file = URL(fileURLWithPath: path).deletingPathExtension().lastPathComponent
        guard let url = Bundle.main.url(forResource: file, withExtension: "png", subdirectory: "Resources/crops") else { return nil }
        return UIImage(contentsOfFile: url.path)
    }
}
