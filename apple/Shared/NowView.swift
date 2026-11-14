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
                Card { FlowDiagram(now: n).frame(height: 300) }
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
        LazyVGrid(columns: [GridItem(.adaptive(minimum: 150), spacing: 10)], spacing: 10) {
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
        Card {
            HStack {
                Image(systemName: icon).foregroundStyle(color)
                CardLabel(label)
            }
            Text(value).font(.system(size: 26, weight: .heavy, design: .rounded)).monospacedDigit()
            Text(sub ?? " ").font(.caption2.weight(.semibold)).textCase(.uppercase).foregroundStyle(color)
        }
    }
}

// Four nodes; the server says how much flows along each edge.
struct FlowDiagram: View {
    let now: Now
    @State private var phase: CGFloat = 0

    var body: some View {
        GeometryReader { g in
            let w = g.size.width, h = g.size.height
            let r: CGFloat = 40
            let solar = CGPoint(x: w / 2, y: r + 4)
            let battery = CGPoint(x: w / 2, y: h - r - 30)
            let gridP = CGPoint(x: max(r + 8, w / 2 - 150), y: (solar.y + battery.y) / 2)
            let home = CGPoint(x: min(w - r - 8, w / 2 + 150), y: (solar.y + battery.y) / 2)
            let paths = now.flow.paths
            let edges: [(String, CGPoint, CGPoint, Color)] = [
                ("solar_home", solar, home, Theme.solar), ("solar_battery", solar, battery, Theme.solar),
                ("solar_grid", solar, gridP, Theme.solar), ("battery_home", battery, home, Theme.battery),
                ("battery_grid", battery, gridP, Theme.battery), ("grid_home", gridP, home, Theme.grid),
                ("grid_battery", gridP, battery, Theme.grid),
            ]
            ZStack {
                ForEach(edges, id: \.0) { key, a, b, color in
                    let kw = paths[key] ?? 0
                    Path { p in p.move(to: a); p.addLine(to: b) }
                        .stroke(kw > 0.01 ? color : Theme.border,
                                style: StrokeStyle(lineWidth: kw > 0.01 ? 2.5 : 1, lineCap: .round,
                                                   dash: kw > 0.01 ? [6, 8] : [], dashPhase: -phase))
                }
                Node(icon: "sun.max.fill", label: "Solar", value: Fmt.kw(now.solarKw), color: Theme.solar, active: now.solarKw > 0.01).position(solar)
                Node(icon: "house.fill", label: "Home", value: Fmt.kw(now.homeKw), color: Theme.home, active: true).position(home)
                Node(icon: "bolt.horizontal.fill", label: "Grid", value: now.grid.direction == "balanced" ? "0.0 kW" : Fmt.kw(now.grid.kw),
                     color: Theme.grid, active: now.grid.direction != "balanced").position(gridP)
                Node(icon: "battery.75percent", label: "Battery", value: Fmt.kw(now.battery?.kw),
                     sub: Fmt.pct(now.battery?.soc), color: Theme.battery, active: now.battery?.state != "idle").position(battery)
                Text(now.flow.headline)
                    .font(.footnote).foregroundStyle(Theme.dim).multilineTextAlignment(.center)
                    .frame(width: w).position(x: w / 2, y: h - 8)
            }
        }
        .onAppear {
            withAnimation(.linear(duration: 1.2).repeatForever(autoreverses: false)) { phase = 14 }
        }
    }
}

struct Node: View {
    let icon: String
    let label: String
    let value: String
    var sub: String?
    let color: Color
    let active: Bool

    var body: some View {
        VStack(spacing: 1) {
            Image(systemName: icon).font(.system(size: 15)).foregroundStyle(color)
            Text(label.uppercased()).font(.system(size: 8, weight: .semibold)).foregroundStyle(Theme.dim)
            Text(value).font(.system(size: 12, weight: .bold)).monospacedDigit()
            if let sub { Text(sub).font(.system(size: 9)).foregroundStyle(Theme.dim) }
        }
        .frame(width: 80, height: 80)
        .background(Circle().fill(Theme.bg))
        .overlay(Circle().strokeBorder(active ? color : Theme.border, lineWidth: 2))
    }
}

struct BatterySection: View {
    @Environment(Store.self) private var store
    let now: Now

    var body: some View {
        let b = now.battery
        SectionHead(title: "SunVault Battery", trailing: b.map { ($0.stale ? "last reading " : "updated ") + Fmt.time($0.fetchedAt) })
        LazyVGrid(columns: [GridItem(.adaptive(minimum: 150), spacing: 10)], spacing: 10) {
            Card(title: "State of charge") {
                Text(Fmt.pct(b?.soc)).font(.system(size: 26, weight: .heavy, design: .rounded))
                ProgressView(value: min(1, max(0, b?.soc ?? 0)))
                    .tint((b?.soc ?? 1) < 0.2 ? Theme.red : (b?.soc ?? 1) < 0.5 ? Theme.accent : Theme.green)
            }
            Card(title: b?.state == "charging" ? "↓ Charging" : b?.state == "discharging" ? "↑ Discharging" : "· Idle") {
                Text(Fmt.kw(b?.kw)).font(.system(size: 26, weight: .heavy, design: .rounded))
                Text(chargeText(b)).font(.caption).foregroundStyle(Theme.dim)
            }
            Card(title: "Backup time") {
                Text(Fmt.backup(b?.backupMin)).font(.system(size: 26, weight: .heavy, design: .rounded))
                Text("at current load").font(.caption).foregroundStyle(Theme.dim)
            }
            if let s = now.houseSides {
                Card(title: "House sides") {
                    Text(String(format: "%.1f · %.1f kW", s.l1Kw, s.l2Kw)).font(.system(size: 22, weight: .heavy, design: .rounded))
                    Text(s.balanced ? "balanced" : String(format: "uneven by %.1f kW · battery covers ~%.1f kW", s.gapKw, s.batteryCoverKw))
                        .font(.caption).foregroundStyle(Theme.dim)
                }
            }
        }
        BatteryModePicker()
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
