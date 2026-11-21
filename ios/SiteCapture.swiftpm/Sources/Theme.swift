import SwiftUI

enum Theme {
    static let navy = Color(red: 0.05, green: 0.11, blue: 0.24)
    static let navyDeep = Color(red: 0.03, green: 0.07, blue: 0.17)
    static let blueprint = Color(red: 0.25, green: 0.56, blue: 1.0)
    static let paper = Color.white
    static let red = Color(red: 0.86, green: 0.18, blue: 0.20)
    static let green = Color(red: 0.10, green: 0.62, blue: 0.36)
    static let grey = Color(red: 0.45, green: 0.50, blue: 0.58)

    static func color(_ v: Verdict) -> Color {
        switch v { case .conflict: return red; case .consistent: return green; case .unclear: return grey }
    }
    static func label(_ v: Verdict) -> String {
        switch v { case .conflict: return "CONFLICT"; case .consistent: return "CONSISTENT"; case .unclear: return "UNCLEAR" }
    }
}

struct VerdictChip: View {
    let verdict: Verdict
    var body: some View {
        Text(Theme.label(verdict))
            .font(.caption.weight(.heavy))
            .tracking(0.6)
            .foregroundStyle(.white)
            .padding(.horizontal, 10).padding(.vertical, 4)
            .background(Theme.color(verdict), in: Capsule())
    }
}

/// Faint blueprint grid used as a backdrop.
struct BlueprintGrid: View {
    var body: some View {
        Canvas { ctx, size in
            var p = Path()
            let step: CGFloat = 28
            var x: CGFloat = 0
            while x < size.width { p.move(to: CGPoint(x: x, y: 0)); p.addLine(to: CGPoint(x: x, y: size.height)); x += step }
            var y: CGFloat = 0
            while y < size.height { p.move(to: CGPoint(x: 0, y: y)); p.addLine(to: CGPoint(x: size.width, y: y)); y += step }
            ctx.stroke(p, with: .color(Theme.blueprint.opacity(0.12)), lineWidth: 0.5)
        }
        .background(Theme.navy)
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
            .background(disabled ? Theme.grey : Theme.blueprint, in: RoundedRectangle(cornerRadius: 14))
            .foregroundStyle(.white)
        }
        .disabled(disabled)
    }
}
