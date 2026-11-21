import SwiftUI

struct ResultsView: View {
    @EnvironmentObject var state: AppState

    var body: some View {
        NavigationStack {
            ZStack {
                BlueprintGrid().ignoresSafeArea()
                if state.cards.isEmpty && state.mock {
                    ScrollView {
                        LazyVStack(spacing: 12) {
                            VStack(alignment: .leading, spacing: 2) {
                                Text("Sample results (recorded)").font(.headline).foregroundStyle(Theme.ink)
                                Text("Recorded from our real drawing set. Not a live result.")
                                    .font(.footnote).foregroundStyle(Theme.ink2)
                            }
                            .frame(maxWidth: .infinity, alignment: .leading)
                            ForEach(SampleData.cards) { card in
                                NavigationLink(value: card) { CardRow(card: card) }.buttonStyle(.plain)
                            }
                        }
                        .padding(16)
                    }
                } else if state.cards.isEmpty {
                    VStack(spacing: 8) {
                        Image(systemName: "doc.text.magnifyingglass").font(.system(size: 44)).foregroundStyle(Theme.blue)
                        Text("No results yet").font(.headline).foregroundStyle(Theme.ink)
                        Text("Pick or record a clip on the Capture tab.")
                            .font(.callout).foregroundStyle(Theme.ink2)
                    }
                } else {
                    ScrollView {
                        LazyVStack(spacing: 12) {
                            if let s = state.videoSeconds {
                                Text("Clip \(String(format: "%.1f", s)) s  |  \(state.cards.count) claims  |  \(state.cards.filter { $0.verdict == .conflict }.count) conflict")
                                    .font(Theme.mono).foregroundStyle(Theme.ink2)
                            }
                            ForEach(state.cards) { card in
                                NavigationLink(value: card) { CardRow(card: card) }.buttonStyle(.plain)
                            }
                        }
                        .padding(16)
                    }
                }
            }
            .navigationTitle("Results")
            .navigationDestination(for: Card.self) { CardDetail(card: $0) }
            .toolbar {
                if !state.cards.isEmpty {
                    Button("Clear") { state.cards = []; state.videoSeconds = nil }
                }
            }
        }
    }
}

func clock(_ s: Double) -> String {
    let t = Int(s); return String(format: "%d:%02d", t / 60, t % 60)
}

struct CardRow: View {
    let card: Card
    var body: some View {
        PaperSheet {
            VStack(alignment: .leading, spacing: 10) {
                HStack(alignment: .top) {
                    VerdictStamp(verdict: card.verdict, confidence: card.confidence, size: 15)
                    Spacer()
                    if let t = card.t_start_s ?? card.video_evidence?.t_s {
                        Text(clock(t)).font(Theme.mono).foregroundStyle(Theme.ink2)
                            .padding(.horizontal, 6).padding(.vertical, 2)
                            .overlay(Rectangle().stroke(Theme.ink, lineWidth: 1.5))
                    }
                }
                .padding(.top, 4)
                Text(card.claim).font(.system(size: 17, weight: .bold)).foregroundStyle(Theme.ink)
                    .multilineTextAlignment(.leading)
                if let r = card.reason {
                    Text(r).font(.subheadline).foregroundStyle(Theme.ink2)
                        .lineLimit(3).multilineTextAlignment(.leading)
                }
            }
        }
    }
}

struct CardDetail: View {
    let card: Card
    var body: some View {
        ZStack {
            BlueprintGrid().ignoresSafeArea()
            ScrollView {
                CardSheet(card: card).padding(16)
            }
        }
        .navigationBarTitleDisplayMode(.inline)
        .navigationTitle("Claim")
    }
}

/// The full paper sheet for one card. Used by Results detail and by the Ask tab.
struct CardSheet: View {
    @EnvironmentObject var state: AppState
    @Environment(\.openURL) private var openURL
    let card: Card

    /// Evidence grouped by crop image, each box numbered by its position in `evidence`.
    private var groups: [(crop: String?, items: [(n: Int, ev: Evidence)])] {
        var order: [String?] = []
        var map: [String?: [(Int, Evidence)]] = [:]
        for (i, ev) in (card.evidence ?? []).enumerated() {
            if map[ev.crop] == nil { order.append(ev.crop) }
            map[ev.crop, default: []].append((i + 1, ev))
        }
        return order.map { (crop: $0, items: (map[$0] ?? []).map { (n: $0.0, ev: $0.1) }) }
    }

    var body: some View {
        PaperSheet {
            VStack(alignment: .leading, spacing: 16) {
                HStack(alignment: .top, spacing: 14) {
                    VerdictStamp(verdict: card.verdict, confidence: card.confidence, size: 22)
                    Spacer(minLength: 0)
                    if let t = card.t_start_s ?? card.video_evidence?.t_s {
                        VStack(alignment: .trailing, spacing: 2) {
                            BlockLabel("Clip time")
                            Text(clock(t)).font(Theme.mono).foregroundStyle(Theme.ink)
                        }
                    }
                }
                .padding(.top, 6)

                if SampleData.isSample(card) {
                    Text("SAMPLE RESULT (RECORDED) - not a live check").font(Theme.mono).foregroundStyle(Theme.ink2)
                }
                Text(card.claim).font(.system(size: 22, weight: .bold)).foregroundStyle(Theme.ink)
                if let r = card.reason {
                    Text(r).font(.body).foregroundStyle(Theme.ink2)
                }

                if let ve = card.video_evidence, let frame = ve.frame {
                    VStack(alignment: .leading, spacing: 6) {
                        BlockLabel("Site video" + (ve.t_s.map { " - " + clock($0) } ?? ""))
                        EvidenceImage(path: frame, boxes: ve.box_2d.map { [(label: "V", box: $0)] } ?? [], tint: Theme.color(card.verdict))
                        if let t = ve.text_read, !t.isEmpty {
                            Text("read: " + t).font(Theme.mono).foregroundStyle(Theme.ink2)
                        }
                    }
                }

                ForEach(Array(groups.enumerated()), id: \.offset) { _, g in
                    VStack(alignment: .leading, spacing: 6) {
                        let first = g.items[0].ev
                        BlockLabel("Sheet " + (first.sheet_number ?? first.sheet ?? "?") + (first.revision.map { "  rev " + $0 } ?? ""))
                        if SampleData.isSample(card) {
                            if let c = g.crop, let img = SampleData.crop(c) {
                                Image(uiImage: img).resizable().scaledToFit()
                                    .overlay(BoxLayer(boxes: g.items.compactMap { i in i.ev.box_2d_on_crop.map { (label: "\(i.n)", box: $0) } }, tint: Theme.red))
                                    .overlay(Rectangle().stroke(Theme.line, lineWidth: 1))
                            }
                        } else if let crop = g.crop {
                            EvidenceImage(path: crop, boxes: g.items.compactMap { i in i.ev.box_2d_on_crop.map { (label: "\(i.n)", box: $0) } }, tint: Theme.red)
                        }
                        ForEach(g.items, id: \.n) { it in
                            if let l = it.ev.label {
                                HStack(alignment: .top, spacing: 8) {
                                    Text("\(it.n)").font(.system(size: 11, weight: .bold, design: .monospaced))
                                        .foregroundStyle(.white).frame(width: 18, height: 18).background(Theme.red)
                                    Text(l).font(.footnote).foregroundStyle(Theme.ink)
                                }
                            }
                        }
                    }
                }

                if let checks = card.checks, !checks.isEmpty {
                    VStack(alignment: .leading, spacing: 8) {
                        BlockLabel("Checks")
                        ForEach(Array(checks.enumerated()), id: \.offset) { _, c in
                            HStack(alignment: .top, spacing: 8) {
                                switch c.passed {
                                case .some(true): Image(systemName: "checkmark.square.fill").foregroundStyle(Theme.green)
                                case .some(false): Image(systemName: "xmark.square.fill").foregroundStyle(Theme.red)
                                case .none: Image(systemName: "questionmark.square.fill").foregroundStyle(Theme.grey)
                                }
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(c.check ?? c.by ?? "check").font(.subheadline.weight(.semibold)).foregroundStyle(Theme.ink)
                                    if let n = c.note { Text(n).font(.footnote).foregroundStyle(Theme.ink2) }
                                }
                            }
                        }
                    }
                }

                Rectangle().fill(Theme.line).frame(height: 1)
                HStack {
                    Text(card.id).font(Theme.mono).foregroundStyle(Theme.ink2).lineLimit(1)
                    Spacer()
                    if !SampleData.isSample(card), let u = state.api.webURL(cardID: card.id) {
                        Button { openURL(u) } label: {
                            Label("Open in web", systemImage: "arrow.up.right.square").font(.footnote.weight(.bold))
                        }
                        .accessibilityLabel("Open in web")
                    }
                }
            }
        }
    }
}

/// A backend image (crop or frame) with numbered boxes drawn on the image itself, so they track its displayed size.
struct EvidenceImage: View {
    @EnvironmentObject var state: AppState
    let path: String
    let boxes: [(label: String, box: [Double])]
    let tint: Color

    var body: some View {
        if let url = state.api.cropURL(path) {
            AsyncImage(url: url) { phase in
                switch phase {
                case .success(let img):
                    img.resizable().scaledToFit().overlay(BoxLayer(boxes: boxes, tint: tint))
                        .overlay(Rectangle().stroke(Theme.line, lineWidth: 1))
                case .failure:
                    Label("Image failed to load", systemImage: "exclamationmark.triangle").foregroundStyle(Theme.red)
                default:
                    ProgressView().frame(maxWidth: .infinity, minHeight: 160)
                }
            }
        }
    }
}

/// Draws boxes given as [ymin, xmin, ymax, xmax] on a 0-1000 scale over whatever view it is attached to.
struct BoxLayer: View {
    let boxes: [(label: String, box: [Double])]
    let tint: Color
    var body: some View {
        GeometryReader { geo in
            ForEach(Array(boxes.enumerated()), id: \.offset) { _, b in
                if b.box.count == 4 {
                    let ymin = b.box[0] / 1000 * geo.size.height
                    let xmin = b.box[1] / 1000 * geo.size.width
                    let ymax = b.box[2] / 1000 * geo.size.height
                    let xmax = b.box[3] / 1000 * geo.size.width
                    Rectangle().stroke(tint, lineWidth: 2.5)
                        .background(tint.opacity(0.10))
                        .frame(width: max(xmax - xmin, 2), height: max(ymax - ymin, 2))
                        .overlay(alignment: .topLeading) {
                            Text(b.label).font(.system(size: 11, weight: .bold, design: .monospaced))
                                .foregroundStyle(.white).padding(.horizontal, 4).background(tint)
                                .offset(x: -1.25, y: -17)
                        }
                        .position(x: (xmin + xmax) / 2, y: (ymin + ymax) / 2)
                }
            }
        }
    }
}
