import SwiftUI

struct ResultsView: View {
    @EnvironmentObject var state: AppState

    var body: some View {
        NavigationStack {
            ZStack {
                BlueprintGrid().ignoresSafeArea()
                if state.cards.isEmpty {
                    VStack(spacing: 8) {
                        Image(systemName: "doc.text.magnifyingglass").font(.system(size: 44)).foregroundStyle(Theme.blueprint)
                        Text("No results yet").font(.headline)
                        Text("Record a clip or type a claim on the Capture tab.")
                            .font(.callout).foregroundStyle(.white.opacity(0.75))
                    }
                } else {
                    ScrollView {
                        LazyVStack(spacing: 12) {
                            if let s = state.videoSeconds {
                                Text("Clip length \(String(format: "%.1f", s)) s  -  \(state.cards.count) claims checked")
                                    .font(.footnote).foregroundStyle(.white.opacity(0.7))
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
        .onAppear { state.loadMockIfNeeded() }
    }
}

func clock(_ s: Double) -> String {
    let t = Int(s); return String(format: "%d:%02d", t / 60, t % 60)
}

struct CardRow: View {
    let card: Card
    var body: some View {
        HStack(spacing: 0) {
            Rectangle().fill(Theme.color(card.verdict)).frame(width: 6)
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    VerdictChip(verdict: card.verdict)
                    if let c = card.confidence {
                        Text("\(Int(c * 100))%").font(.caption.weight(.semibold)).foregroundStyle(Theme.grey)
                    }
                    Spacer()
                    if let t = card.t_start_s {
                        Label(clock(t), systemImage: "play.circle").font(.caption.monospacedDigit()).foregroundStyle(Theme.grey)
                    }
                }
                Text(card.claim).font(.headline).foregroundStyle(Theme.navy).multilineTextAlignment(.leading)
                if let r = card.reason {
                    Text(r).font(.subheadline).foregroundStyle(Theme.navy.opacity(0.75))
                        .lineLimit(3).multilineTextAlignment(.leading)
                }
            }
            .padding(14)
            Spacer(minLength: 0)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Theme.paper, in: RoundedRectangle(cornerRadius: 14))
        .clipShape(RoundedRectangle(cornerRadius: 14))
    }
}

struct CardDetail: View {
    @EnvironmentObject var state: AppState
    let card: Card

    var body: some View {
        ZStack {
            Theme.navy.ignoresSafeArea()
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    HStack { VerdictChip(verdict: card.verdict)
                        if let c = card.confidence { Text("confidence \(Int(c * 100))%").font(.footnote).foregroundStyle(.white.opacity(0.7)) }
                        Spacer()
                        if let t = card.t_start_s { Text("at \(clock(t))").font(.footnote.monospacedDigit()).foregroundStyle(.white.opacity(0.7)) }
                    }
                    Text(card.claim).font(.title3.weight(.bold))
                    if let r = card.reason { Text(r).font(.body).foregroundStyle(.white.opacity(0.9)) }

                    ForEach(Array((card.evidence ?? []).enumerated()), id: \.offset) { _, ev in
                        EvidenceView(ev: ev)
                    }

                    if let checks = card.checks, !checks.isEmpty {
                        Text("CHECKS").font(.caption.weight(.heavy)).tracking(1).foregroundStyle(Theme.blueprint)
                        VStack(alignment: .leading, spacing: 10) {
                            ForEach(Array(checks.enumerated()), id: \.offset) { _, c in
                                HStack(alignment: .top, spacing: 10) {
                                    switch c.passed {
                                    case .some(true): Image(systemName: "checkmark.circle.fill").foregroundStyle(Theme.green)
                                    case .some(false): Image(systemName: "xmark.circle.fill").foregroundStyle(Theme.red)
                                    case .none: Image(systemName: "questionmark.circle.fill").foregroundStyle(Theme.grey)
                                    }
                                    VStack(alignment: .leading, spacing: 2) {
                                        Text(c.check ?? c.by ?? "check").font(.subheadline.weight(.semibold))
                                        if c.check != nil, let by = c.by { Text(by).font(.caption).foregroundStyle(.white.opacity(0.6)) }
                                        if let n = c.note { Text(n).font(.footnote).foregroundStyle(.white.opacity(0.8)) }
                                    }
                                }
                            }
                        }
                    }
                }
                .padding(16)
            }
        }
        .navigationBarTitleDisplayMode(.inline)
        .navigationTitle("Claim")
    }
}

struct EvidenceView: View {
    @EnvironmentObject var state: AppState
    let ev: Evidence

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("SHEET \(ev.sheet ?? "?")" + (ev.revision.map { "  REV \($0)" } ?? ""))
                .font(.caption.weight(.heavy)).tracking(1).foregroundStyle(Theme.blueprint)
            if state.mock {
                MockCrop().overlay(BoxOverlay(box: ev.box_2d_on_crop, label: ev.label))
                    .aspectRatio(1.4, contentMode: .fit)
            } else if let crop = ev.crop, let url = state.api.cropURL(crop) {
                AsyncImage(url: url) { phase in
                    switch phase {
                    case .success(let img):
                        // The overlay is attached to the image itself, so boxes track the displayed crop size.
                        img.resizable().scaledToFit().overlay(BoxOverlay(box: ev.box_2d_on_crop, label: ev.label))
                    case .failure:
                        Label("Crop failed to load", systemImage: "exclamationmark.triangle").foregroundStyle(Theme.red)
                    default:
                        ProgressView().frame(maxWidth: .infinity, minHeight: 160)
                    }
                }
            }
            if let l = ev.label { Text(l).font(.footnote).foregroundStyle(.white.opacity(0.8)) }
        }
        .padding(10)
        .background(Theme.navyDeep, in: RoundedRectangle(cornerRadius: 12))
        .overlay(RoundedRectangle(cornerRadius: 12).stroke(Theme.blueprint.opacity(0.4)))
    }
}

/// Draws box_2d_on_crop = [ymin, xmin, ymax, xmax] (0-1000) over whatever view it is attached to.
struct BoxOverlay: View {
    let box: [Double]?
    let label: String?
    var body: some View {
        GeometryReader { geo in
            if let b = box, b.count == 4 {
                let ymin = b[0] / 1000 * geo.size.height
                let xmin = b[1] / 1000 * geo.size.width
                let ymax = b[2] / 1000 * geo.size.height
                let xmax = b[3] / 1000 * geo.size.width
                Rectangle()
                    .stroke(Theme.red, lineWidth: 3)
                    .background(Theme.red.opacity(0.12))
                    .frame(width: max(xmax - xmin, 1), height: max(ymax - ymin, 1))
                    .position(x: (xmin + xmax) / 2, y: (ymin + ymax) / 2)
            }
        }
    }
}

/// Stand-in drawing for mock mode (the sample crops do not exist on any server).
struct MockCrop: View {
    var body: some View {
        ZStack {
            Color(red: 0.93, green: 0.95, blue: 0.99)
            Canvas { ctx, size in
                var p = Path()
                for i in 1..<10 {
                    let x = size.width * CGFloat(i) / 10, y = size.height * CGFloat(i) / 10
                    p.move(to: CGPoint(x: x, y: 0)); p.addLine(to: CGPoint(x: x, y: size.height))
                    p.move(to: CGPoint(x: 0, y: y)); p.addLine(to: CGPoint(x: size.width, y: y))
                }
                ctx.stroke(p, with: .color(Theme.blueprint.opacity(0.25)), lineWidth: 0.5)
            }
            Text("sample crop").font(.caption).foregroundStyle(Theme.navy.opacity(0.4))
        }
    }
}
