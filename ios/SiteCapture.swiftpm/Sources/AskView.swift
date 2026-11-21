import SwiftUI

/// Free text (a claim, a question, an RFI request) -> POST /api/ask -> a card.
struct AskView: View {
    @EnvironmentObject var state: AppState
    @State private var text = ""

    private var trimmed: String { text.trimmingCharacters(in: .whitespacesAndNewlines) }

    var body: some View {
        NavigationStack {
            ZStack {
                BlueprintGrid().ignoresSafeArea()
                ScrollView {
                    VStack(spacing: 14) {
                        PaperSheet {
                            VStack(alignment: .leading, spacing: 10) {
                                BlockLabel("Ask the drawings")
                                TextField("e.g. All water closets in Toilet 105A are WC-2", text: $text, axis: .vertical)
                                    .lineLimit(2...5)
                                    .padding(10)
                                    .background(Theme.board2)
                                    .overlay(Rectangle().stroke(Theme.line, lineWidth: 1))
                                    .foregroundStyle(Theme.ink)
                                PrimaryButton(title: state.busy ? "Checking..." : "Ask", systemImage: "questionmark.bubble", disabled: state.busy || trimmed.isEmpty) {
                                    Task { if await state.ask(trimmed) { text = "" } }
                                }
                                if state.busy { ProgressView().frame(maxWidth: .infinity) }
                                if let e = state.error {
                                    Text(e).font(.footnote).foregroundStyle(Theme.red)
                                }
                            }
                        }
                        ForEach(state.asked) { c in
                            CardSheet(card: c)
                        }
                    }
                    .padding(16)
                }
                .scrollDismissesKeyboard(.interactively)
            }
            .navigationTitle("Ask")
            .navigationBarTitleDisplayMode(.inline)
        }
    }
}
