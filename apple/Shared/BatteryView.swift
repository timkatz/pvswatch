import Charts
import SwiftUI

// The SunVault screen: a drawing of the Hub+ and battery cabinets, the
// mode and backup reserve, the last 24 h, and each unit. All from
// /api/v1/battery.
struct BatteryView: View {
    @Environment(Store.self) private var store

    var body: some View {
        Page {
            if let d = store.battery {
                Card { SunVaultIllustration(doc: d).frame(height: 230).frame(maxWidth: 560).frame(maxWidth: .infinity) }
                Summary(doc: d)
                ModeCards(doc: d)
                ReserveCard(doc: d)
                if !d.day.isEmpty { DayCharts(doc: d) }
                UnitsCard(doc: d)
            } else {
                EmptyState()
            }
        }
        .refreshable { await store.refreshLive() }
    }
}

// Hub+ on the left, one cabinet per unit, charge level, reserve line, and
// arrows that show which way energy is moving.
private struct SunVaultIllustration: View {
    let doc: BatteryDoc
    @State private var flow: CGFloat = 0

    var body: some View {
        let b = doc.battery
        let units = doc.units.isEmpty ? [nil] : doc.units.map(Optional.some)
        let state = b?.state ?? "idle"
        let gridColor = doc.gridState.kind == "backup" ? Theme.red : doc.gridState.kind == "on_grid" ? Theme.green : Theme.dim
        GeometryReader { g in
            HStack(alignment: .bottom, spacing: 18) {
                // Hub+
                VStack(spacing: 6) {
                    ZStack {
                        RoundedRectangle(cornerRadius: 8).fill(Color(hex: 0x1C2633))
                        RoundedRectangle(cornerRadius: 8).strokeBorder(Theme.border, lineWidth: 1.5)
                        VStack(spacing: 8) {
                            Text("Hub+").font(.system(size: 11, weight: .bold)).foregroundStyle(Theme.dim)
                            Circle().fill(gridColor).frame(width: 10, height: 10)
                                .shadow(color: gridColor, radius: 4)
                            Text(doc.gridState.kind == "backup" ? "backup" : doc.gridState.kind == "on_grid" ? "on grid" : "?")
                                .font(.system(size: 9)).foregroundStyle(Theme.dim)
                        }
                    }
                    .frame(width: 70, height: g.size.height * 0.45)
                    Text("Home \(Fmt.kw(doc.homeKw))").font(.caption2).foregroundStyle(Theme.dim)
                }

                // Energy moving between the Hub+ and the cabinets
                FlowArrows(state: state, phase: flow)
                    .frame(width: 34, height: 40)
                    .padding(.bottom, g.size.height * 0.3)

                ForEach(Array(units.enumerated()), id: \.offset) { i, u in
                    Cabinet(soc: u?.soc ?? b?.soc ?? 0, reserve: doc.reserve.value, state: state,
                            label: units.count > 1 ? "Unit \(i + 1)" : "SunVault",
                            sub: u.map { "\(Fmt.kw($0.kw)) · \($0.tempC.map { "\(Int($0))°C" } ?? "")" })
                        .frame(width: min(110, (g.size.width - 140) / CGFloat(units.count) - 14),
                               height: g.size.height - 20)
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .bottom)
        }
        .onAppear { withAnimation(.linear(duration: 1).repeatForever(autoreverses: false)) { flow = 1 } }
    }
}

private struct Cabinet: View {
    let soc: Double
    let reserve: Double?
    let state: String
    let label: String
    let sub: String?

    var body: some View {
        VStack(spacing: 6) {
            GeometryReader { g in
                let h = g.size.height, inner = h - 24
                ZStack(alignment: .bottom) {
                    RoundedRectangle(cornerRadius: 14).fill(Color(hex: 0xE9ECEF).opacity(0.08))
                    RoundedRectangle(cornerRadius: 14).strokeBorder(Color(hex: 0x3A4656), lineWidth: 2)
                    // charge level
                    RoundedRectangle(cornerRadius: 8)
                        .fill(LinearGradient(colors: [fillColor.opacity(0.95), fillColor.opacity(0.55)],
                                             startPoint: .bottom, endPoint: .top))
                        .frame(height: max(4, inner * min(1, max(0, soc))))
                        .padding(.horizontal, 12).padding(.bottom, 12)
                        .animation(.easeInOut(duration: 0.8), value: soc)
                    // reserve line
                    if let r = reserve, r > 0 {
                        VStack(spacing: 2) {
                            Text("reserve \(Int((r * 100).rounded()))%").font(.system(size: 8, weight: .semibold)).foregroundStyle(Theme.accent)
                            Rectangle().fill(Theme.accent).frame(height: 1.5).padding(.horizontal, 6)
                        }
                        .padding(.bottom, 12 + inner * r - 6)
                    }
                    // brand strip + percentage
                    VStack {
                        Capsule().fill(Color(hex: 0x3A4656)).frame(width: 26, height: 4).padding(.top, 8)
                        Spacer()
                        Text(Fmt.pct(soc)).font(.system(size: 20, weight: .heavy, design: .rounded))
                            .foregroundStyle(.white).shadow(radius: 3)
                        Spacer()
                    }
                }
            }
            Text(label).font(.caption.weight(.semibold))
            Text(sub ?? " ").font(.caption2).foregroundStyle(Theme.dim).lineLimit(1)
        }
    }

    private var fillColor: Color { soc < 0.2 ? Theme.red : soc < 0.5 ? Theme.accent : Theme.green }
}

private struct FlowArrows: View {
    let state: String
    let phase: CGFloat
    var body: some View {
        let charging = state == "charging", active = state != "idle"
        HStack(spacing: 2) {
            ForEach(0..<3) { i in
                Image(systemName: charging ? "chevron.right" : "chevron.left")
                    .font(.system(size: 13, weight: .bold))
                    .foregroundStyle(active ? (charging ? Theme.green : Theme.accent) : Theme.border)
                    .opacity(active ? 0.35 + 0.65 * Double((phase * 3 + CGFloat(charging ? i : 2 - i)).truncatingRemainder(dividingBy: 3) / 2) : 1)
            }
        }
    }
}

private struct Summary: View {
    let doc: BatteryDoc
    var body: some View {
        let b = doc.battery
        TileGrid(wide: 3) {
            StatTile(label: "Charge", value: Fmt.pct(b?.soc),
                     sub: b.map { $0.stale ? "last reading \(Fmt.time($0.fetchedAt))" : "updated \(Fmt.time($0.fetchedAt))" })
            StatTile(label: b?.state == "charging" ? "↓ Charging" : b?.state == "discharging" ? "↑ Discharging" : "· Idle",
                     value: Fmt.kw(b?.kw), sub: source(b))
            StatTile(label: "Backup time", value: Fmt.backup(b?.backupMin), sub: "at current load")
        }
    }

    private func source(_ b: Now.Battery?) -> String {
        guard let b else { return " " }
        switch b.state {
        case "charging": return ["solar": "storing solar", "solar+grid": "from solar + grid", "grid": "from grid"][b.chargeSource ?? ""] ?? " "
        case "discharging": return "powering home"
        default: return "standby"
        }
    }
}

private struct ModeCards: View {
    @Environment(Store.self) private var store
    let doc: BatteryDoc

    var body: some View {
        SectionHead(title: "Battery mode", trailing: store.modeMessage)
        TileGrid(wide: 3) {
            ForEach(doc.mode.options, id: \.self) { o in
                let on = doc.mode.value == o.value
                Button {
                    // The current mode isn't disabled (that greys it out); a tap just does nothing.
                    if !on { Task { await store.setBatteryMode(o.value, label: o.label) } }
                } label: {
                    HStack(alignment: .top, spacing: 12) {
                        Image(systemName: on ? "largecircle.fill.circle" : "circle")
                            .foregroundStyle(on ? Theme.accent : Theme.dim).font(.title3)
                        VStack(alignment: .leading, spacing: 3) {
                            Text(o.label).font(.subheadline.weight(.semibold)).foregroundStyle(Theme.text)
                            Text(o.description).font(.caption).foregroundStyle(Theme.dim)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        Spacer()
                    }
                    .padding(12)
                    .frame(maxWidth: .infinity, minHeight: 96, maxHeight: 96, alignment: .topLeading)
                    .background(Theme.card, in: RoundedRectangle(cornerRadius: 12))
                    .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(on ? Theme.accent : Theme.border, lineWidth: on ? 1.5 : 1))
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .disabled(store.modeBusy)
            }
        }
    }
}

private struct ReserveCard: View {
    @Environment(Store.self) private var store
    let doc: BatteryDoc
    @State private var draft: Double?

    var body: some View {
        let current = (doc.reserve.value ?? 0) * 100
        let value = draft ?? current
        Card(title: "Backup reserve", trailing: store.reserveMessage) {
            HStack(alignment: .firstTextBaseline) {
                Text("\(Int(value.rounded()))%").font(.system(size: 26, weight: .heavy, design: .rounded))
                Text("kept for outages").font(.caption).foregroundStyle(Theme.dim)
                Spacer()
                if let draft, Int(draft.rounded()) != Int(current.rounded()) {
                    Button("Set \(Int(draft.rounded()))%") {
                        Task { await store.setReserve(pct: Int(draft.rounded())); self.draft = nil }
                    }
                    .buttonStyle(.borderedProminent).tint(Theme.accent).foregroundStyle(.black)
                    .disabled(store.reserveBusy)
                }
            }
            Slider(value: Binding(get: { value }, set: { draft = $0 }),
                   in: doc.reserve.min * 100...doc.reserve.max * 100, step: 5)
            Text("In Self-Supply and Cost Savings the battery stops discharging at this level, so it's there when the grid goes down.")
                .font(.caption).foregroundStyle(Theme.dim)
        }
    }
}

private struct DayCharts: View {
    let doc: BatteryDoc
    @State private var sel: Date?
    var body: some View {
        let hit = nearest(doc.day, to: sel, by: \.date)
        Card(title: "Last 24 hours") {
            Chart {
                ForEach(doc.day.filter { $0.soc != nil }) { p in
                    AreaMark(x: .value("t", p.date), y: .value("%", (p.soc ?? 0) * 100))
                        .foregroundStyle(Theme.green.opacity(0.22))
                    LineMark(x: .value("t", p.date), y: .value("%", (p.soc ?? 0) * 100))
                        .foregroundStyle(Theme.green)
                }
                if let r = doc.reserve.value {
                    RuleMark(y: .value("reserve", r * 100))
                        .foregroundStyle(Theme.accent).lineStyle(StrokeStyle(lineWidth: 1, dash: [4, 3]))
                }
                if let p = hit {
                    RuleMark(x: .value("t", p.date)).foregroundStyle(Theme.dim.opacity(0.6))
                        .annotation(position: .top, spacing: 0, overflowResolution: .init(x: .fit(to: .chart), y: .fit(to: .chart))) {
                            ChartTip(title: p.date.tipLabel, rows: [
                                ("Charge", Fmt.pct(p.soc), Theme.green),
                                ("Battery", flowText(p.kw, plus: "discharging", minus: "charging"), (p.kw ?? 0) < 0 ? Theme.green : Theme.accent),
                            ])
                        }
                }
            }
            .chartXSelection(value: $sel)
            .chartYScale(domain: 0...100)
            .chartYAxisLabel("charge %")
            .chartXAxis { AxisMarks(values: .automatic(desiredCount: 5)) { _ in AxisGridLine(); AxisValueLabel(format: .dateTime.hour()) } }
            .frame(height: 130)
            Chart {
                ForEach(doc.day.filter { $0.kw != nil }) { p in
                    BarMark(x: .value("t", p.date), y: .value("kW", -(p.kw ?? 0)))
                        .foregroundStyle((p.kw ?? 0) < 0 ? Theme.green : Theme.accent)
                }
                if let p = hit { RuleMark(x: .value("t", p.date)).foregroundStyle(Theme.dim.opacity(0.6)) }
            }
            .chartXSelection(value: $sel)
            .chartYAxisLabel("kW  (+ charging)")
            .chartXAxis { AxisMarks(values: .automatic(desiredCount: 5)) { _ in AxisGridLine(); AxisValueLabel(format: .dateTime.hour()) } }
            .frame(height: 110)
        }
    }
}

private struct UnitsCard: View {
    let doc: BatteryDoc
    var body: some View {
        if !doc.units.isEmpty {
            SectionHead(title: "Units")
            TileGrid(wide: 2) {
                ForEach(Array(doc.units.enumerated()), id: \.offset) { i, u in
                    Card(title: "Unit \(i + 1)", trailing: u.serial.map { String($0.suffix(6)) }) {
                        Row("Charge", Fmt.pct(u.soc))
                        Row("Power", Fmt.kw(u.kw))
                        Row("Health", u.health.map { Fmt.pct($0) } ?? "not reported")
                        Row("Cell / inverter temp", "\(u.tempC.map { "\(Int($0))°C" } ?? "—") / \(u.inverterTempC.map { "\(Int($0))°C" } ?? "—")")
                        Row("Voltage", u.voltage.map { String(format: "%.1f V", $0) })
                        Row("Limits (charge / discharge)", "\(Fmt.kw(u.chargeLimitKw)) / \(Fmt.kw(u.dischargeLimitKw))")
                        Row("Lifetime in / out", "\(Fmt.kwh(u.chargedKwh)) / \(Fmt.kwh(u.dischargedKwh))")
                    }
                }
            }
            if let rt = doc.lifetime.roundTripPct {
                Card(title: "Lifetime") {
                    Row("Charged", Fmt.kwh(doc.lifetime.chargedKwh))
                    Row("Discharged", Fmt.kwh(doc.lifetime.dischargedKwh))
                    Row("Round trip", String(format: "%.0f%%", rt))
                }
            }
        }
    }
}
