import SwiftUI

struct NowView: View {
    @Environment(Store.self) private var store
    /// Leave out the battery section (laptop posture shows it on the other leaf).
    var compact = false

    var body: some View {
        Page {
            if let n = store.now {
                StatusStrip(now: n)
                PowerTiles(now: n)
                FlowCard(now: n)
                if let t = store.today { TodayTiles(t: t) }
                if !compact { BatterySection(now: n) }
            } else {
                EmptyState()
            }
        }
        .refreshable { await store.refreshAll() }
    }
}

struct EmptyState: View {
    @Environment(Store.self) private var store
    var body: some View {
        VStack(spacing: 10) {
            if store.api == nil {
                Image(systemName: "sun.max").font(.largeTitle).foregroundStyle(Theme.accent)
                Text("Add your pvswatch server in Settings").foregroundStyle(Theme.dim)
            } else if let e = store.error {
                Image(systemName: "exclamationmark.triangle").font(.largeTitle).foregroundStyle(Theme.red)
                Text(e).foregroundStyle(Theme.dim).multilineTextAlignment(.center)
            } else {
                ProgressView()
                Text("Loading…").foregroundStyle(Theme.dim)
            }
        }
        .frame(maxWidth: .infinity, minHeight: 300)
    }
}

// Grid/backup state + time-of-use now, one line each.
struct StatusStrip: View {
    @Environment(Store.self) private var store
    let now: Now

    var body: some View {
        let kind = store.sunvault?.kind ?? now.gridState.kind
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                Circle().fill(kind == "backup" ? Theme.red : kind == "on_grid" ? Theme.green : Theme.dim).frame(width: 8, height: 8)
                Text(kind == "backup" ? "SunVault on backup power" : kind == "on_grid" ? "SunVault on grid" : "Grid state unknown")
                    .font(.footnote.weight(.semibold))
                Spacer()
                if let e = store.error {
                    Label(e, systemImage: "wifi.exclamationmark").font(.caption2).foregroundStyle(Theme.red).lineLimit(1)
                } else if let t = now.dataTime {
                    Text("PVS \(Fmt.time(t))").font(.caption2).foregroundStyle(Theme.dim)
                }
            }
            if let p = store.tou?.now {
                HStack(spacing: 4) {
                    Text(p.name).font(.footnote.weight(.semibold))
                    Text("now · \(Int((p.rate * 100).rounded()))¢/kWh").font(.footnote).foregroundStyle(Theme.dim)
                    if let nx = p.next {
                        Text("· \(nx.name) at \(Fmt.hour(nx.start))").font(.footnote).foregroundStyle(Theme.dim).lineLimit(1)
                    }
                }
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Theme.card, in: RoundedRectangle(cornerRadius: 12))
        .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(kind == "backup" ? Theme.red : Theme.border))
    }
}

struct PowerTiles: View {
    let now: Now

    var body: some View {
        let b = now.battery
        TileGrid {
            Tile(icon: "sun.max.fill", label: "Solar", value: Fmt.kw(now.solarKw), color: Theme.solar)
            Tile(icon: "house.fill", label: "Home", value: Fmt.kw(now.homeKw), color: Theme.home)
            Tile(icon: "battery.75percent", label: "Battery", value: Fmt.kw(b?.kw),
                 sub: b.map { "\($0.state) · \(Fmt.pct($0.soc))" }, color: Theme.battery)
            Tile(icon: "bolt.horizontal.fill", label: "Grid",
                 value: now.grid.direction == "balanced" ? "0.0 kW" : Fmt.kw(now.grid.kw),
                 sub: now.grid.direction, color: now.grid.direction == "exporting" ? Theme.green : Theme.grid)
        }
    }
}

struct Tile: View {
    let icon: String
    let label: String
    let value: String
    var sub: String?
    var color: Color

    var body: some View {
        StatTile(icon: icon, label: label, value: value, sub: sub?.uppercased(), color: color, subColor: color)
    }
}

// Power flow: home in the middle, solar above, grid left, battery right.
// Same drawing as the web (600×420 design space). Active edges glow in the
// source's color and thicken with kW; particles run faster with more power.
private enum FD {
    static let size = CGSize(width: 600, height: 440)
    static let top: CGFloat = 32   // headroom so the solar halo isn't clipped
    struct Node { let x, y, r: CGFloat; let icon, label: String; let color: Color }
    static let solar = Node(x: 300, y: 62, r: 46, icon: "sun.max.fill", label: "SOLAR", color: Theme.solar)
    static let grid = Node(x: 92, y: 232, r: 46, icon: "bolt.horizontal.fill", label: "GRID", color: Theme.grid)
    static let battery = Node(x: 508, y: 232, r: 46, icon: "battery.75percent", label: "BATTERY", color: Theme.green)
    static let home = Node(x: 300, y: 232, r: 60, icon: "house.fill", label: "HOME", color: Theme.home)

    static func line(_ a: CGPoint, _ b: CGPoint) -> Path { Path { $0.move(to: a); $0.addLine(to: b) } }
    static func curve(_ a: CGPoint, _ c1: CGPoint, _ c2: CGPoint, _ b: CGPoint) -> Path {
        Path { $0.move(to: a); $0.addCurve(to: b, control1: c1, control2: c2) }
    }
    /// Path length in design-space points (sampled once).
    static func length(_ key: String) -> Double {
        if let l = lengths[key] { return l }
        guard let e = edges.first(where: { $0.key == key }) else { return 100 }
        var total = 0.0, last: CGPoint?
        for i in 0...60 {
            guard let pt = e.path.trimmedPath(from: 0, to: max(0.001, Double(i) / 60)).currentPoint else { continue }
            if let l = last { total += hypot(pt.x - l.x, pt.y - l.y) }
            last = pt
        }
        lengths[key] = total
        return total
    }
    nonisolated(unsafe) static var lengths: [String: Double] = [:]

    static let edges: [(key: String, path: Path, color: Color, track: Bool)] = [
        // Source-to-home lines stop at the ring's outer edge.
        ("solar_home", line(.init(x: 300, y: 110), .init(x: 300, y: 160)), Theme.solar, true),
        ("grid_home", line(.init(x: 140, y: 232), .init(x: 228, y: 232)), Theme.grid, true),
        ("battery_home", line(.init(x: 454, y: 232), .init(x: 372, y: 232)), Theme.green, true),
        ("solar_battery", curve(.init(x: 344, y: 80), .init(x: 430, y: 88), .init(x: 500, y: 128), .init(x: 508, y: 184)), Theme.solar, true),
        // Exports are green, like the grid's "exporting" state.
        ("solar_grid", curve(.init(x: 256, y: 80), .init(x: 170, y: 88), .init(x: 100, y: 128), .init(x: 92, y: 184)), Theme.green, true),
        ("grid_battery", curve(.init(x: 92, y: 280), .init(x: 110, y: 392), .init(x: 490, y: 392), .init(x: 508, y: 280)), Theme.grid, true),
        ("battery_grid", curve(.init(x: 508, y: 280), .init(x: 490, y: 392), .init(x: 110, y: 392), .init(x: 92, y: 280)), Theme.green, false),
    ]
}

struct FlowDiagram: View {
    let now: Now

    var body: some View {
        GeometryReader { g in
                let k = min(g.size.width / FD.size.width, g.size.height / FD.size.height)
                let dx = (g.size.width - FD.size.width * k) / 2, dy = (g.size.height - FD.size.height * k) / 2
                let xf = CGAffineTransform(translationX: dx, y: dy).scaledBy(x: k, y: k).translatedBy(x: 0, y: FD.top)
                let paths = now.flow.paths
                ZStack {
                    // tracks + active lines
                    ForEach(FD.edges, id: \.key) { e in
                        let kw = paths[e.key] ?? 0
                        if e.track {
                            e.path.applying(xf).stroke(Color.white.opacity(0.06), style: StrokeStyle(lineWidth: 3 * k, lineCap: .round))
                        }
                        if kw > 0.01 {
                            e.path.applying(xf)
                                .stroke(e.color.opacity(0.9), style: StrokeStyle(lineWidth: min(7, 2 + kw * 0.9) * k, lineCap: .round))
                                .shadow(color: e.color.opacity(0.8), radius: 4 * k)
                        }
                    }
                    // particles
                    TimelineView(.animation) { tl in
                        let t = tl.date.timeIntervalSinceReferenceDate
                        Canvas { ctx, _ in
                            for e in FD.edges {
                                let kw = paths[e.key] ?? 0
                                guard kw > 0.01 else { continue }
                                // Calm, distance-based pace (design-space px/s), one
                                // particle per ~90 px so short links don't bunch up.
                                let len = FD.length(e.key)
                                let speed = 18 + min(40, kw * 6)
                                let dots = max(1, min(3, Int((len / 90).rounded())))
                                let r = min(6, 3.5 + kw * 0.4) * k
                                let p = e.path.applying(xf)
                                for i in 0..<dots {
                                    let f = (t * speed / len + Double(i) / Double(dots)).truncatingRemainder(dividingBy: 1)
                                    guard let pt = p.trimmedPath(from: 0, to: max(0.001, f)).currentPoint else { continue }
                                    let fade = min(1, min(f, 1 - f) * 6)
                                    var c = ctx
                                    c.opacity = fade
                                    c.addFilter(.shadow(color: e.color, radius: 5 * k))
                                    c.fill(Path(ellipseIn: CGRect(x: pt.x - r, y: pt.y - r, width: 2 * r, height: 2 * r)), with: .color(e.color))
                                }
                            }
                        }
                    }
                    node(FD.solar, k: k, xf: xf, value: Fmt.kw(now.solarKw), sub: now.solarKw > 0.01 ? "producing" : nil, active: now.solarKw > 0.01)
                    // Grid glows red when importing, green when exporting.
                    node(FD.Node(x: FD.grid.x, y: FD.grid.y, r: FD.grid.r, icon: FD.grid.icon, label: FD.grid.label,
                                 color: now.grid.direction == "exporting" ? Theme.green : Theme.grid),
                         k: k, xf: xf, value: now.grid.direction == "balanced" ? "0.0 kW" : Fmt.kw(now.grid.kw),
                         sub: now.grid.direction == "balanced" ? nil : now.grid.direction, active: now.grid.direction != "balanced")
                    socRing(k: k, xf: xf)
                    node(FD.battery, k: k, xf: xf, value: Fmt.kw(now.battery?.kw),
                         sub: now.battery.map { Fmt.pct($0.soc) + ($0.state == "idle" ? "" : " · \($0.state)") },
                         active: (now.battery?.state ?? "idle") != "idle")
                    homeNode(k: k, xf: xf)
                }
        }
        .aspectRatio(FD.size.width / FD.size.height, contentMode: .fit)
    }

    /// Scaled font size that never drops below `floor` points.
    private func fs(_ base: CGFloat, _ k: CGFloat, floor: CGFloat) -> CGFloat { max(base * k, floor) }

    private func node(_ n: FD.Node, k: CGFloat, xf: CGAffineTransform, value: String, sub: String?, active: Bool) -> some View {
        let c = CGPoint(x: n.x, y: n.y).applying(xf)
        return ZStack {
            if active {
                Circle().fill(RadialGradient(colors: [n.color.opacity(0.25), .clear], center: .center,
                                             startRadius: n.r * k * 0.8, endRadius: (n.r + 26) * k))
                    .frame(width: (n.r + 26) * 2 * k, height: (n.r + 26) * 2 * k)
            }
            Circle().fill(Color(hex: 0x111A26))
                .overlay(Circle().strokeBorder(active ? n.color.opacity(0.45) : Color.white.opacity(0.07), lineWidth: 1))
                .frame(width: n.r * 2 * k, height: n.r * 2 * k)
            VStack(spacing: 1) {
                if k >= 0.75 { Image(systemName: n.icon).font(.system(size: 18 * k)).foregroundStyle(n.color) }
                Text(n.label).font(.system(size: fs(9, k, floor: 8), weight: .bold)).tracking(1).foregroundStyle(Theme.dim)
                Text(value).font(.system(size: fs(15, k, floor: 12), weight: .heavy)).monospacedDigit()
                if let sub, k >= 0.7 { Text(sub).font(.system(size: fs(9, k, floor: 8), weight: .semibold)).foregroundStyle(Theme.dim) }
            }
            .lineLimit(1).minimumScaleFactor(0.8)
            .frame(width: n.r * 2 * k - 6)
        }
        .position(c)
    }

    // Battery charge as a thin ring hugging the battery node.
    private func socRing(k: CGFloat, xf: CGAffineTransform) -> some View {
        let n = FD.battery
        let soc = min(1, max(0, now.battery?.soc ?? 0))
        let color = soc < 0.2 ? Theme.red : soc < 0.5 ? Theme.accent : Theme.green
        return ProgressRing(value: soc, color: color, diameter: (n.r + 6) * 2 * k, width: 3.5 * k)
            .position(CGPoint(x: n.x, y: n.y).applying(xf))
    }

    private func homeNode(k: CGFloat, xf: CGAffineTransform) -> some View {
        let n = FD.home
        let c = CGPoint(x: n.x, y: n.y).applying(xf)
        let pct = (now.flow.selfPoweredPct ?? 0) / 100
        let ring = (n.r + 6) * 2 * k
        return ZStack {
            Circle().fill(RadialGradient(colors: [n.color.opacity(0.22), .clear], center: .center,
                                         startRadius: n.r * k * 0.8, endRadius: (n.r + 30) * k))
                .frame(width: (n.r + 30) * 2 * k, height: (n.r + 30) * 2 * k)
            ProgressRing(value: pct, color: Theme.solar, diameter: ring, width: 3.5 * k)
            Circle().fill(Color(hex: 0x111A26))
                .overlay(Circle().strokeBorder(Color.white.opacity(0.07), lineWidth: 1))
                .frame(width: n.r * 2 * k, height: n.r * 2 * k)
            VStack(spacing: 2) {
                if k >= 0.75 { Image(systemName: n.icon).font(.system(size: 22 * k)).foregroundStyle(n.color) }
                Text(n.label).font(.system(size: fs(9, k, floor: 8), weight: .bold)).tracking(1).foregroundStyle(Theme.dim)
                Text(Fmt.kw(now.homeKw)).font(.system(size: fs(21, k, floor: 15), weight: .heavy)).monospacedDigit()
                if let sp = now.flow.selfPoweredPct, k >= 0.7 {
                    Text("\(Int(sp))% self-powered").font(.system(size: fs(9, k, floor: 8), weight: .semibold)).foregroundStyle(Theme.dim)
                }
            }
            .lineLimit(1).minimumScaleFactor(0.8)
            .frame(width: n.r * 2 * k - 8)
        }
        .position(c)
    }
}

struct BatterySection: View {
    @Environment(Store.self) private var store
    let now: Now
    /// The mode buttons; only the laptop posture's control deck shows them here.
    var showPicker = false

    var body: some View {
        let b = now.battery
        SectionHead(title: "SunVault", trailing: b.map { ($0.stale ? "last reading " : "updated ") + Fmt.time($0.fetchedAt) })
        TileGrid(wide: 3) {
            StatTile(label: "State of charge", value: Fmt.pct(b?.soc)) {
                ProgressView(value: min(1, max(0, b?.soc ?? 0)))
                    .tint((b?.soc ?? 1) < 0.2 ? Theme.red : (b?.soc ?? 1) < 0.5 ? Theme.accent : Theme.green)
                if let label = b?.modeLabel ?? store.currentMode.flatMap(modeLabel) { ModeChip(label: label) }
            }
            StatTile(label: b?.state == "charging" ? "↓ Charging" : b?.state == "discharging" ? "↑ Discharging" : "· Idle",
                     value: Fmt.kw(b?.kw), sub: chargeText(b))
            StatTile(label: "Backup time", value: Fmt.backup(b?.backupMin), sub: "at current load")
        }
        if showPicker { BatteryModePicker() }
    }

    private func chargeText(_ b: Now.Battery?) -> String {
        guard let b else { return " " }
        switch b.state {
        case "charging":
            return ["solar": "storing solar", "solar+grid": "from solar + grid", "grid": "from grid"][b.chargeSource ?? ""] ?? " "
        case "discharging": return "powering home"
        default: return "standby"
        }
    }
}

func modeLabel(_ value: String) -> String? { batteryModes.first { $0.value == value }?.label }

struct ModeChip: View {
    let label: String
    var body: some View {
        Text("\(label) mode").font(.caption2.weight(.semibold)).foregroundStyle(Theme.accent)
            .padding(.horizontal, 8).padding(.vertical, 2)
            .background(Theme.accent.opacity(0.15), in: Capsule())
    }
}

struct BatteryModePicker: View {
    @Environment(Store.self) private var store

    var body: some View {
        Card(title: "Mode") {
            HStack(spacing: 8) {
                ForEach(batteryModes, id: \.value) { m in
                    let on = store.currentMode == m.value
                    Button {
                        Task { await store.setBatteryMode(m.value, label: m.label) }
                    } label: {
                        Text(m.label).font(.footnote.weight(.semibold))
                            .padding(.horizontal, 12).padding(.vertical, 6)
                            .background(on ? Theme.accent : .clear, in: Capsule())
                            .overlay(Capsule().strokeBorder(on ? Theme.accent : Theme.border))
                            .foregroundStyle(on ? Color.black : Theme.dim)
                    }
                    .buttonStyle(.plain)
                    .disabled(store.modeBusy || on)
                }
            }
            if let msg = store.modeMessage {
                Text(msg).font(.caption).foregroundStyle(msg.hasPrefix("Not") ? Theme.red : Theme.dim)
            }
        }
    }
}


// Since midnight: what the sun made, what the house used, and what it saved.
struct TodayTiles: View {
    let t: History.Totals
    var body: some View {
        SectionHead(title: "Today")
        TileGrid {
            StatTile(icon: "sun.max.fill", label: "Solar made", value: Fmt.kwh(t.solarKwh), sub: nil, color: Theme.solar)
            StatTile(icon: "house.fill", label: "Home used", value: Fmt.kwh(t.homeKwh), sub: nil, color: Theme.home)
            StatTile(icon: "leaf.fill", label: "Independence", value: t.independencePct.map { "\(Int($0.rounded()))%" } ?? "—",
                     sub: "solar made ÷ home used", color: Theme.green)
            StatTile(icon: "dollarsign.circle", label: "Saved", value: Fmt.money(t.savingsDollars, cents: true),
                     sub: (t.gridNetKwh ?? 0) >= 0 ? "\(Fmt.kwh(t.gridImportKwh)) from the grid" : "\(Fmt.kwh(t.gridExportKwh)) sent to the grid",
                     color: Theme.green, valueColor: Theme.green)
        }
    }
}


// The Now screen's flow card: the diagram, plus where the home's power
// comes from (and where the rest goes). Side by side when there's room.
struct FlowCard: View {
    let now: Now

    var body: some View {
        Card(title: "Energy flow", trailing: now.fetchedAt.map { "live · " + Fmt.time($0) }) {
            ViewThatFits(in: .horizontal) {
                HStack(alignment: .center, spacing: 28) {
                    FlowDiagram(now: now).frame(minWidth: 460, maxWidth: 680)
                    FlowLegend(now: now).frame(width: 250)
                }
                .frame(minWidth: 760)
                VStack(spacing: 14) {
                    FlowDiagram(now: now).frame(maxWidth: 560)
                    FlowLegend(now: now)
                }
            }
            .frame(maxWidth: .infinity)
        }
    }
}

struct FlowLegend: View {
    let now: Now

    var body: some View {
        let p = now.flow.paths
        let sources: [(String, Double, Color)] = [("Solar", p["solar_home"] ?? 0, Theme.solar),
                                                  ("Battery", p["battery_home"] ?? 0, Theme.green),
                                                  ("Grid", p["grid_home"] ?? 0, Theme.grid)]
        let total = max(0.001, sources.reduce(0) { $0 + $1.1 })
        let elsewhere: [(String, Double, Color)] = [
            ("Charging the battery", (p["solar_battery"] ?? 0) + (p["grid_battery"] ?? 0), Theme.green),
            ("Sent to the grid", (p["solar_grid"] ?? 0) + (p["battery_grid"] ?? 0), Theme.solar),
        ].filter { $0.1 > 0.05 }
        VStack(alignment: .leading, spacing: 10) {
            CardLabel("Your home is using")
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Text(Fmt.kw(now.homeKw)).font(.system(size: 28, weight: .heavy, design: .rounded)).monospacedDigit()
                if let sp = now.flow.selfPoweredPct {
                    Text("\(Int(sp))% self-powered").font(.caption.weight(.semibold)).foregroundStyle(Theme.green)
                }
            }
            ForEach(sources, id: \.0) { name, kw, color in
                VStack(alignment: .leading, spacing: 4) {
                    HStack {
                        Circle().fill(color).frame(width: 8, height: 8)
                        Text("From \(name.lowercased())").font(.footnote)
                        Spacer()
                        Text(kw > 0.005 ? Fmt.kw(kw) : "—").font(.footnote.weight(.semibold)).monospacedDigit()
                        Text("\(Int((kw / total * 100).rounded()))%").font(.caption).foregroundStyle(Theme.dim)
                            .frame(width: 38, alignment: .trailing)
                    }
                    GeometryReader { g in
                        Capsule().fill(Color.white.opacity(0.06))
                            .overlay(alignment: .leading) {
                                Capsule().fill(color).frame(width: g.size.width * min(1, kw / total))
                            }
                    }
                    .frame(height: 5)
                }
            }
            if !elsewhere.isEmpty {
                Divider().overlay(Theme.border)
                ForEach(elsewhere, id: \.0) { name, kw, color in
                    HStack {
                        Image(systemName: "arrow.turn.down.right").font(.caption).foregroundStyle(color)
                        Text(name).font(.footnote)
                        Spacer()
                        Text(Fmt.kw(kw)).font(.footnote.weight(.semibold)).monospacedDigit()
                    }
                }
            }
            Text(now.flow.headline).font(.caption).foregroundStyle(Theme.dim)
                .padding(.horizontal, 10).padding(.vertical, 6)
                .background(Color.white.opacity(0.04), in: Capsule())
                .overlay(Capsule().strokeBorder(Theme.border))
        }
    }
}


// A thin progress ring with a faint track, a soft gradient and a glow.
struct ProgressRing: View {
    let value: Double
    let color: Color
    let diameter: CGFloat
    let width: CGFloat

    var body: some View {
        ZStack {
            Circle().stroke(Color.white.opacity(0.06), lineWidth: width)
            Circle().trim(from: 0, to: max(0.001, value))
                .stroke(AngularGradient(colors: [color.opacity(0.55), color], center: .center,
                                        startAngle: .degrees(0), endAngle: .degrees(360 * max(0.05, value))),
                        style: StrokeStyle(lineWidth: width, lineCap: .round))
                .rotationEffect(.degrees(-90))
                .shadow(color: color.opacity(0.6), radius: width * 1.2)
                .animation(.easeInOut(duration: 0.8), value: value)
        }
        .frame(width: diameter, height: diameter)
    }
}
