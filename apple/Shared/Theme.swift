import Charts
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

// Stat tiles in even columns that fill the width: `wide` columns on a
// regular-width screen, 2 on a compact one. Every tile is the same height.
struct TileGrid<Content: View>: View {
    var wide = 4
    @ViewBuilder var content: Content
    #if os(iOS)
    @Environment(\.horizontalSizeClass) private var hsc
    private var count: Int { hsc == .compact ? min(2, wide) : wide }
    #else
    private var count: Int { wide }
    #endif

    var body: some View {
        LazyVGrid(columns: Array(repeating: GridItem(.flexible(), spacing: 10), count: count), spacing: 10) {
            content
        }
    }
}

// One number with a label and a line under it, at a fixed height so tiles line up.
struct StatTile<Footer: View>: View {
    var icon: String?
    let label: String
    let value: String
    var color: Color = Theme.text
    var valueColor: Color = Theme.text
    @ViewBuilder var footer: Footer

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                if let icon { Image(systemName: icon).foregroundStyle(color).font(.footnote) }
                CardLabel(label).lineLimit(1)
            }
            Text(value).font(.system(size: 26, weight: .heavy, design: .rounded)).monospacedDigit()
                .foregroundStyle(valueColor).lineLimit(1).minimumScaleFactor(0.6)
                .layoutPriority(1)
            footer
            Spacer(minLength: 0)
        }
        .padding(14)
        .frame(maxWidth: .infinity, minHeight: 128, maxHeight: 128, alignment: .topLeading)
        .background(Theme.card, in: RoundedRectangle(cornerRadius: 12))
        .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(Theme.border))
    }
}

extension StatTile where Footer == SubLine {
    init(icon: String? = nil, label: String, value: String, sub: String?, color: Color = Theme.text,
         subColor: Color = Theme.dim, valueColor: Color = Theme.text) {
        self.icon = icon; self.label = label; self.value = value; self.color = color; self.valueColor = valueColor
        self.footer = SubLine(text: sub, color: subColor)
    }
}

struct SubLine: View {
    let text: String?
    var color: Color = Theme.dim
    var body: some View {
        Text(text ?? " ").font(.caption).foregroundStyle(color).lineLimit(2)
    }
}

// The box a chart shows for the point under the pointer (hover on Mac,
// touch-and-drag on iPhone).
struct ChartTip: View {
    let title: String
    let rows: [(label: String, value: String, color: Color)]

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(title).font(.caption2.weight(.semibold)).foregroundStyle(Theme.text)
            ForEach(rows.indices, id: \.self) { i in
                HStack(spacing: 6) {
                    Circle().fill(rows[i].color).frame(width: 7, height: 7)
                    Text(rows[i].label).font(.caption2).foregroundStyle(Theme.dim)
                    Spacer(minLength: 8)
                    Text(rows[i].value).font(.caption2.weight(.semibold)).monospacedDigit()
                }
            }
        }
        .padding(8)
        .frame(minWidth: 140)
        .background(Theme.bg.opacity(0.95), in: RoundedRectangle(cornerRadius: 8))
        .overlay(RoundedRectangle(cornerRadius: 8).strokeBorder(Theme.border))
    }
}

/// The item whose date is closest to `date`.
func nearest<T>(_ items: [T], to date: Date?, by key: (T) -> Date) -> T? {
    guard let date else { return nil }
    return items.min { abs(key($0).timeIntervalSince(date)) < abs(key($1).timeIntervalSince(date)) }
}

extension Date {
    var tipLabel: String { formatted(.dateTime.month(.abbreviated).day().hour().minute()) }
}

// The app's own modal: a dark card over a dimmed backdrop, inside the
// window (not a system sheet). Close with ×, a click outside, or Esc.
struct AppModal<Item: Identifiable, Panel: View>: ViewModifier {
    @Binding var item: Item?
    let panel: (Item) -> Panel

    func body(content: Content) -> some View {
        content.overlay {
            if let current = item {
                ZStack {
                    Color.black.opacity(0.6).ignoresSafeArea()
                        .onTapGesture { item = nil }
                    panel(current)
                        .frame(maxWidth: 760)
                        .background(Theme.bg, in: RoundedRectangle(cornerRadius: 18))
                        .clipShape(RoundedRectangle(cornerRadius: 18))
                        .overlay(RoundedRectangle(cornerRadius: 18).strokeBorder(Theme.border))
                        .shadow(color: .black.opacity(0.5), radius: 30, y: 10)
                        .padding(.horizontal, 16).padding(.vertical, 24)
                        .transition(.scale(scale: 0.97).combined(with: .opacity))
                }
                #if os(macOS)
                .onExitCommand { item = nil }
                #endif
            }
        }
        .animation(.snappy(duration: 0.2), value: item?.id)
    }
}

extension View {
    func appModal<Item: Identifiable, Panel: View>(item: Binding<Item?>, @ViewBuilder _ panel: @escaping (Item) -> Panel) -> some View {
        modifier(AppModal(item: item, panel: panel))
    }
}

// Title row for an AppModal: title, subtitle, and a round close button.
struct ModalHeader: View {
    let title: String
    var subtitle: String?
    let close: () -> Void
    var body: some View {
        HStack(alignment: .firstTextBaseline) {
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.title3.weight(.bold))
                if let subtitle { Text(subtitle).font(.caption).foregroundStyle(Theme.dim) }
            }
            Spacer()
            Button(action: close) {
                Image(systemName: "xmark").font(.system(size: 12, weight: .bold))
                    .frame(width: 28, height: 28)
                    .background(Theme.card, in: Circle())
                    .overlay(Circle().strokeBorder(Theme.border))
            }
            .buttonStyle(.plain)
            .keyboardShortcut(.cancelAction)
        }
        .padding(.horizontal, 18).padding(.top, 16).padding(.bottom, 4)
    }
}

extension View {
    /// Chart selection that follows the pointer: hover on Mac (Charts only
    /// selects on click there), touch-and-drag on iPhone.
    func chartHover<V: Plottable>(_ sel: Binding<V?>) -> some View {
        #if os(macOS)
        chartOverlay { proxy in
            GeometryReader { g in
                Rectangle().fill(.clear).contentShape(Rectangle())
                    .onContinuousHover { phase in
                        switch phase {
                        case .active(let p):
                            guard let plot = proxy.plotFrame else { return }
                            sel.wrappedValue = proxy.value(atX: p.x - g[plot].origin.x, as: V.self)
                        case .ended:
                            sel.wrappedValue = nil
                        }
                    }
            }
        }
        #else
        chartXSelection(value: sel)
        #endif
    }
}
