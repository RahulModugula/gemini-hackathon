import SwiftUI

/// Same visual language as the web UI (ui/index.html): drafting board, paper sheet, ink, double-ruled stamps.
/// No gradients, no emoji.
enum Theme {
    static let board = Color(red: 0.894, green: 0.910, blue: 0.906)      // #e4e8e7
    static let board2 = Color(red: 0.945, green: 0.953, blue: 0.949)     // #f1f3f2
    static let paper = Color(red: 0.984, green: 0.988, blue: 0.984)      // #fbfcfb
    static let line = Color(red: 0.667, green: 0.702, blue: 0.722)       // #aab3b8
    static let ink = Color(red: 0.078, green: 0.098, blue: 0.118)        // #14191e
    static let ink2 = Color(red: 0.275, green: 0.318, blue: 0.357)       // #46515b
    static let red = Color(red: 0.784, green: 0.149, blue: 0.106)        // #c8261b
    static let green = Color(red: 0.071, green: 0.475, blue: 0.290)      // #12794a
    static let grey = Color(red: 0.420, green: 0.463, blue: 0.502)       // #6b7680
    static let blue = Color(red: 0.122, green: 0.373, blue: 0.839)       // #1f5fd6
    static let amber = Color(red: 0.420, green: 0.278, blue: 0.0)        // #6b4700

    static func color(_ v: Verdict) -> Color {
        switch v { case .conflict: return red; case .consistent: return green; case .unclear: return grey }
    }
    static func label(_ v: Verdict) -> String {
        switch v { case .conflict: return "Conflict"; case .consistent: return "Consistent"; case .unclear: return "Unclear" }
    }
    static let mono = Font.system(.footnote, design: .monospaced)
}

/// Rubber-stamp verdict: double rule, uppercase, tilted a little.
struct VerdictStamp: View {
    let verdict: Verdict
    var confidence: Double? = nil
    var size: CGFloat = 20
    var body: some View {
        let c = Theme.color(verdict)
        VStack(spacing: 1) {
            Text(Theme.label(verdict).uppercased())
                .font(.system(size: size, weight: .heavy)).tracking(1.2)
            if let confidence {
                Text("\(Int(confidence * 100))% confidence")
                    .font(.system(size: max(size * 0.5, 10), weight: .semibold))
            }
        }
        .foregroundStyle(c)
        .padding(.horizontal, 12).padding(.vertical, 5)
        .overlay(RoundedRectangle(cornerRadius: 3).stroke(c, lineWidth: 1.5))
        .padding(3)
        .overlay(RoundedRectangle(cornerRadius: 5).stroke(c, lineWidth: 1.5))
        .rotationEffect(.degrees(-2))
        .fixedSize()
    }
}

/// Small caps label used for title-block headings.
struct BlockLabel: View {
    let text: String
    init(_ t: String) { text = t }
    var body: some View {
        Text(text.uppercased()).font(.system(size: 11, weight: .bold)).tracking(1).foregroundStyle(Theme.ink2)
    }
}

/// A sheet of paper: hairline border, ink rule on top like a title block.
struct PaperSheet<Content: View>: View {
    @ViewBuilder var content: Content
    var body: some View {
        content
            .padding(14)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Theme.paper)
            .overlay(Rectangle().stroke(Theme.line, lineWidth: 1))
            .overlay(alignment: .top) { Rectangle().fill(Theme.ink).frame(height: 3) }
    }
}

/// Faint drafting grid behind everything.
struct BlueprintGrid: View {
    var body: some View {
        Canvas { ctx, size in
            var p = Path()
            let step: CGFloat = 28
            var x: CGFloat = 0
            while x < size.width { p.move(to: CGPoint(x: x, y: 0)); p.addLine(to: CGPoint(x: x, y: size.height)); x += step }
            var y: CGFloat = 0
            while y < size.height { p.move(to: CGPoint(x: 0, y: y)); p.addLine(to: CGPoint(x: size.width, y: y)); y += step }
            ctx.stroke(p, with: .color(Theme.blue.opacity(0.07)), lineWidth: 0.5)
        }
        .background(Theme.board)
    }
}

struct PrimaryButton: View {
    let title: String
    var systemImage: String? = nil
    var disabled = false
    let action: () -> Void
    var body: some View {
        Button(action: action) {
            HStack {
                if let s = systemImage { Image(systemName: s) }
                Text(title).fontWeight(.bold)
            }
            .frame(maxWidth: .infinity).padding(.vertical, 14)
            .background(disabled ? Theme.grey : Theme.ink, in: RoundedRectangle(cornerRadius: 3))
            .foregroundStyle(.white)
        }
        .disabled(disabled)
    }
}
