import SwiftUI

// The dashboard's :root tokens (solar_dashboard.html).
enum Theme {
    static let bg = Color(hex: 0x0C1117)
    static let card = Color(hex: 0x151D27)
    static let border = Color(hex: 0x1E2A3A)
    static let text = Color(hex: 0xE4E8EE)
    static let dim = Color(hex: 0x7A8A9E)
    static let accent = Color(hex: 0xF59E0B)
    static let green = Color(hex: 0x22C55E)
    static let red = Color(hex: 0xEF4444)
    static let blue = Color(hex: 0x3B82F6)
    static let cyan = Color(hex: 0x06B6D4)

    static let solar = accent
    static let home = blue
    static let battery = green
    static let grid = red
}

extension Color {
    init(hex: UInt32) {
        self.init(red: Double((hex >> 16) & 0xFF) / 255, green: Double((hex >> 8) & 0xFF) / 255, blue: Double(hex & 0xFF) / 255)
    }
}

enum Fmt {
    static func kw(_ v: Double?) -> String {
        guard let v else { return "—" }
        return abs(v) < 1 && abs(v) >= 0.0005 ? "\(Int((abs(v) * 1000).rounded())) W" : String(format: "%.1f kW", abs(v))
    }
    static func kwh(_ v: Double?) -> String {
        guard let v else { return "—" }
        return abs(v) >= 1000 ? String(format: "%.2f MWh", v / 1000) : String(format: "%.1f kWh", v)
    }
    static func pct(_ v: Double?) -> String {
        guard let v else { return "—" }
        return "\(Int((v * 100).rounded()))%"
    }
    static func money(_ v: Double?, cents: Bool = false) -> String {
        guard let v else { return "—" }
        let s = cents ? String(format: "$%.2f", abs(v)) : "$" + Int(abs(v).rounded()).formatted()
        return v < 0 ? "−" + s : s
    }
    static func duration(_ s: Double) -> String {
        let m = Int(s / 60)
        if m < 1 { return "\(Int(s))s" }
        if m < 60 { return "\(m)m" }
        let h = m / 60
        if h < 48 { return m % 60 == 0 ? "\(h)h" : "\(h)h \(m % 60)m" }
        return "\(h / 24)d \(h % 24)h"
    }
    static func backup(_ min: Double?) -> String {
        guard let min else { return "—" }
        return min >= 120 ? String(format: "%.1f hrs", min / 60) : "\(Int(min)) min"
    }
    static func time(_ t: Double?) -> String {
        guard let t else { return "—" }
        return Date(timeIntervalSince1970: t).formatted(date: .omitted, time: .shortened)
    }
    static func dateTime(_ t: Double?) -> String {
        guard let t else { return "—" }
        return Date(timeIntervalSince1970: t).formatted(.dateTime.month(.abbreviated).day().hour().minute())
    }
    static func hour(_ h: Int) -> String {
        let h = h % 24
        return h == 0 ? "12 AM" : h < 12 ? "\(h) AM" : h == 12 ? "12 PM" : "\(h - 12) PM"
    }
}

// A dashboard card.
struct Card<Content: View>: View {
    var title: String?
    var trailing: String?
    @ViewBuilder var content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if title != nil || trailing != nil {
                HStack {
                    if let title { CardLabel(title) }
                    Spacer()
                    if let trailing { Text(trailing).font(.caption2).foregroundStyle(Theme.dim) }
                }
            }
            content
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Theme.card, in: RoundedRectangle(cornerRadius: 12))
        .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(Theme.border))
    }
}

struct CardLabel: View {
    let text: String
    init(_ text: String) { self.text = text }
    var body: some View {
        Text(text.uppercased())
            .font(.system(size: 10, weight: .semibold))
            .tracking(0.6)
            .foregroundStyle(Theme.dim)
    }
}

struct SectionHead: View {
    let title: String
    var trailing: String?
    var body: some View {
        HStack {
            Text(title.uppercased()).font(.caption.weight(.semibold)).tracking(1).foregroundStyle(Theme.dim)
            Spacer()
            if let trailing { Text(trailing).font(.caption2).foregroundStyle(Theme.dim) }
        }
        .padding(.top, 8)
    }
}

// A scrolling page with the app's background and width cap.
struct Page<Content: View>: View {
    @ViewBuilder var content: Content
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) { content }
                .padding(16)
                .frame(maxWidth: 980)
                .frame(maxWidth: .infinity)
        }
        .background(Theme.bg)
        .scrollContentBackground(.hidden)
    }
}
