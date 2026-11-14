import Charts
import SwiftUI

// ── History ──────────────────────────────────────────────────────────
struct HistoryView: View {
    @Environment(Store.self) private var store
    private let ranges = [("6h", "6H"), ("24h", "24H"), ("7d", "7D"), ("30d", "30D"), ("90d", "90D")]

    var body: some View {
        @Bindable var store = store
        Page {
            Picker("Range", selection: $store.historyRange) {
                ForEach(ranges, id: \.0) { Text($0.1).tag($0.0) }
            }
            .pickerStyle(.segmented)

            if let h = store.history {
                if let t = h.periodTotals { Totals(t: t) }
                Card(title: "Power") { PowerChart(history: h, outages: store.outages?.outages ?? []).frame(height: 260) }
                Card(title: "Battery charge") { SocChart(history: h).frame(height: 140) }
            } else {
                EmptyState()
            }
        }
        .refreshable { await store.loadHistory() }
    }
}

private struct Totals: View {
    let t: History.Totals
    var body: some View {
        LazyVGrid(columns: [GridItem(.adaptive(minimum: 140), spacing: 10)], spacing: 10) {
            Tile(icon: "sun.max.fill", label: "Solar", value: Fmt.kwh(t.solarKwh), color: Theme.solar)
            Tile(icon: "house.fill", label: "Home", value: Fmt.kwh(t.homeKwh), color: Theme.home)
            Tile(icon: "bolt.horizontal.fill", label: "Grid", value: Fmt.kwh(abs(t.gridNetKwh ?? 0)),
                 sub: (t.gridNetKwh ?? 0) >= 0 ? "imported" : "exported", color: Theme.grid)
            Tile(icon: "leaf.fill", label: "Independence",
                 value: t.independencePct.map { "\(Int($0.rounded()))%" } ?? "—",
                 sub: t.savingsDollars.map { Fmt.money($0, cents: true) + " saved" }, color: Theme.green)
        }
    }
}

struct PowerChart: View {
    let history: History
    let outages: [Outages.Outage]

    var body: some View {
        let r = history.readings
        let first = r.first?.t ?? 0, last = r.last?.t ?? 0
        Chart {
            ForEach(outages.filter { ($0.end ?? last) >= first && $0.start <= last }) { o in
                RectangleMark(xStart: .value("t", Date(timeIntervalSince1970: max(o.start, first))),
                              xEnd: .value("t", Date(timeIntervalSince1970: min(o.end ?? last, last))))
                    .foregroundStyle((o.kind == "grid" ? Theme.red : Theme.dim).opacity(0.2))
            }
            ForEach(r) { x in
                if let v = x.productionKw {
                    AreaMark(x: .value("t", x.date), y: .value("kW", v), series: .value("s", "Solar"))
                        .foregroundStyle(Theme.solar.opacity(0.15))
                    LineMark(x: .value("t", x.date), y: .value("kW", v), series: .value("s", "Solar"))
                        .foregroundStyle(Theme.solar)
                }
                if let v = x.consumptionKw {
                    LineMark(x: .value("t", x.date), y: .value("kW", v), series: .value("s", "Home"))
                        .foregroundStyle(Theme.home)
                }
                if let v = x.batteryKw {
                    LineMark(x: .value("t", x.date), y: .value("kW", v), series: .value("s", "Battery"))
                        .foregroundStyle(Theme.cyan).lineStyle(StrokeStyle(lineWidth: 1.2, dash: [4, 3]))
                }
                if let v = x.netKw {
                    LineMark(x: .value("t", x.date), y: .value("kW", v), series: .value("s", "Grid"))
                        .foregroundStyle(Theme.green).lineStyle(StrokeStyle(lineWidth: 1.2, dash: [2, 3]))
                }
            }
        }
        .chartForegroundStyleScale(["Solar": Theme.solar, "Home": Theme.home, "Battery": Theme.cyan, "Grid": Theme.green])
        .chartLegend(position: .top)
        .chartXAxis {
            AxisMarks(values: .automatic(desiredCount: 5)) { _ in
                AxisGridLine()
                AxisValueLabel(format: last - first > 2 * 86400
                    ? .dateTime.month(.abbreviated).day() : .dateTime.hour())
            }
        }
        .chartYAxisLabel("kW")
    }
}

private struct SocChart: View {
    let history: History
    var body: some View {
        Chart(history.readings.filter { $0.batterySoc != nil }) { x in
            AreaMark(x: .value("t", x.date), y: .value("%", (x.batterySoc ?? 0) * 100))
                .foregroundStyle(Theme.green.opacity(0.25))
            LineMark(x: .value("t", x.date), y: .value("%", (x.batterySoc ?? 0) * 100))
                .foregroundStyle(Theme.green)
        }
        .chartYScale(domain: 0...100)
    }
}

// ── Money ────────────────────────────────────────────────────────────
struct MoneyView: View {
    @Environment(Store.self) private var store

    var body: some View {
        Page {
            if let tou = store.tou, tou.enabled { TouCard(tou: tou) }
            if let f = store.finance {
                SectionHead(title: "Net cost & savings")
                if let h = f.headline { FinanceTiles(h: h) }
                Card(title: "By bill") {
                    ForEach(f.rows.suffix(12).reversed()) { r in
                        HStack {
                            Text(monthLabel(r.key)).font(.footnote)
                            if r.inProgress == true { Text("so far").font(.caption2).foregroundStyle(Theme.dim) }
                            Spacer()
                            Text(Fmt.money(r.total)).font(.footnote).monospacedDigit()
                            Text(r.saved.map { ($0 >= 0 ? "saved " : "lost ") + Fmt.money(abs($0)) } ?? "—")
                                .font(.caption).monospacedDigit()
                                .foregroundStyle((r.saved ?? 0) >= 0 ? Theme.green : Theme.red)
                                .frame(width: 100, alignment: .trailing)
                        }
                        Divider().overlay(Theme.border)
                    }
                }
            } else if store.tou == nil {
                EmptyState()
            }
        }
        .refreshable { await store.loadMoney() }
    }

    private func monthLabel(_ key: String) -> String {
        let p = key.split(separator: "-").compactMap { Int($0) }
        guard p.count == 2, let d = Calendar.current.date(from: DateComponents(year: p[0], month: p[1])) else { return key }
        return d.formatted(.dateTime.month(.abbreviated).year())
    }
}

private struct FinanceTiles: View {
    let h: Finance.Headline
    var body: some View {
        LazyVGrid(columns: [GridItem(.adaptive(minimum: 200), spacing: 10)], spacing: 10) {
            if let y = h.ytd {
                MoneyTile(label: "\(y.year) so far", value: y.saved,
                          sub: y.prev?.saved.map { "vs \(Fmt.money($0)) by now in \(y.prev!.year)" } ?? "\(y.bills) bills")
            }
            if let l = h.lifetime {
                MoneyTile(label: "Since \(l.since?.prefix(7) ?? "start")", value: l.saved,
                          sub: "paid \(Fmt.money(l.paid)) vs ~\(Fmt.money(l.noSolar)) without solar")
            }
            if let c = h.netCost12, let n = h.bills12, n > 0 {
                Card(title: "Paid, last \(n) bills") {
                    Text(Fmt.money(c)).font(.system(size: 26, weight: .heavy, design: .rounded))
                    Text("\(Fmt.money(c / Double(n)))/mo, utility + solar").font(.caption).foregroundStyle(Theme.dim)
                }
            }
        }
    }
}

private struct MoneyTile: View {
    let label: String
    let value: Double?
    let sub: String
    var body: some View {
        Card(title: label) {
            Text(Fmt.money(value)).font(.system(size: 26, weight: .heavy, design: .rounded))
                .foregroundStyle((value ?? 0) >= 0 ? Theme.green : Theme.red)
            Text(sub).font(.caption).foregroundStyle(Theme.dim)
        }
    }
}

struct TouCard: View {
    let tou: Tou
    var body: some View {
        Card(title: "Time of use", trailing: tou.todayLabel.map { "today: \($0)" }) {
            if let p = tou.now {
                HStack(alignment: .firstTextBaseline) {
                    Text(p.name).font(.headline)
                    Text("\(Int((p.rate * 100).rounded()))¢/kWh").foregroundStyle(Theme.dim)
                    Spacer()
                    if p.cheapest == true { Text("good time for heavy loads").font(.caption).foregroundStyle(Theme.green) }
                    if p.priciest == true { Text("priciest now").font(.caption).foregroundStyle(Theme.red) }
                }
                if let nx = p.next {
                    Text("\(nx.name) (\(Int((nx.rate * 100).rounded()))¢) at \(Fmt.hour(nx.start))")
                        .font(.caption).foregroundStyle(Theme.dim)
                }
            }
            if let segs = tou.today, !segs.isEmpty {
                let rates = Array(Set(segs.map(\.rate))).sorted()
                GeometryReader { g in
                    HStack(spacing: 1) {
                        ForEach(segs, id: \.self) { s in
                            let rank = Double(rates.firstIndex(of: s.rate) ?? 0) / Double(max(1, rates.count - 1))
                            Rectangle().fill(rank > 0.66 ? Theme.red : rank > 0.33 ? Theme.accent : Theme.green)
                                .opacity(0.75)
                                .frame(width: max(0, g.size.width * CGFloat(s.end - s.start) / 24 - 1))
                        }
                    }
                }
                .frame(height: 14).clipShape(RoundedRectangle(cornerRadius: 4))
                HStack {
                    ForEach([0, 6, 12, 18, 24], id: \.self) { h in
                        Text(Fmt.hour(h)).font(.system(size: 9)).foregroundStyle(Theme.dim)
                        if h < 24 { Spacer() }
                    }
                }
            }
        }
    }
}

// ── Outages ──────────────────────────────────────────────────────────
struct OutagesView: View {
    @Environment(Store.self) private var store

    var body: some View {
        Page {
            if let o = store.outages {
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 150), spacing: 10)], spacing: 10) {
                    Card(title: "Power outages · 30d") {
                        Text("\(o.summary.grid.count)").font(.system(size: 26, weight: .heavy, design: .rounded))
                        Text(Fmt.duration(o.summary.grid.totalS) + " total").font(.caption).foregroundStyle(Theme.dim)
                    }
                    Card(title: "PVS offline · 30d") {
                        Text("\(o.summary.pvs.count)").font(.system(size: 26, weight: .heavy, design: .rounded))
                        Text(Fmt.duration(o.summary.pvs.totalS) + " total").font(.caption).foregroundStyle(Theme.dim)
                    }
                }
                Card(title: "Events") {
                    if o.outages.isEmpty { Text("None in the last 30 days").foregroundStyle(Theme.dim) }
                    ForEach(o.outages) { e in
                        HStack(alignment: .top) {
                            Circle().fill(e.kind == "grid" ? Theme.red : Theme.dim).frame(width: 8, height: 8).padding(.top, 5)
                            VStack(alignment: .leading, spacing: 2) {
                                Text(title(e) + (e.ongoing ? " (ongoing)" : "")).font(.footnote.weight(.semibold))
                                Text("\(Fmt.dateTime(e.start)) – \(e.ongoing ? "now" : Fmt.time(e.end))")
                                    .font(.caption).foregroundStyle(Theme.dim)
                            }
                            Spacer()
                            Text(Fmt.duration(e.durationS)).font(.footnote).monospacedDigit()
                        }
                        Divider().overlay(Theme.border)
                    }
                }
            } else {
                EmptyState()
            }
        }
        .refreshable { await store.loadOutages() }
    }

    private func title(_ e: Outages.Outage) -> String {
        if e.kind == "grid" { return "Power outage" }
        switch e.cause {
        case "pvs_restart": return "PVS restarted"
        case "network": return "PVS network dropout"
        default: return "PVS offline"
        }
    }
}

// ── Panels ───────────────────────────────────────────────────────────
struct PanelsView: View {
    @Environment(Store.self) private var store

    var body: some View {
        Page {
            if let p = store.now?.panels {
                SectionHead(title: p.summary, trailing: p.errors > 0 ? "\(p.errors) in error" : nil)
                if p.mode == "night" {
                    Card { Label("Panels are offline after dark — powerline comms need daylight. This is normal.", systemImage: "moon.stars")
                        .font(.footnote).foregroundStyle(Theme.cyan) }
                } else if p.mode == "comms_error" {
                    Card { Label("No communication with the inverters. Check the PVS.", systemImage: "exclamationmark.triangle")
                        .font(.footnote).foregroundStyle(Theme.red) }
                }
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 104), spacing: 8)], spacing: 8) {
                    ForEach(p.items) { PanelCell(p: $0) }
                }
            } else {
                EmptyState()
            }
        }
    }
}

private struct PanelCell: View {
    let p: Now.Panel
    var body: some View {
        VStack(spacing: 3) {
            Text(p.status == "ok" ? "\(Int(p.w ?? 0)) W" : p.status == "idle" ? "Idle" : "Error")
                .font(.system(size: 17, weight: .bold, design: .rounded))
                .foregroundStyle(p.status == "error" ? Theme.red : p.status == "idle" ? Theme.dim : Theme.text)
            if let t = p.tempC, p.status == "ok" { Text("\(Int(t))°C").font(.caption2).foregroundStyle(Theme.dim) }
            Text(String(p.serial.suffix(6))).font(.system(size: 9)).foregroundStyle(Theme.dim)
            if let l = p.lifetimeKwh { Text(Fmt.kwh(l)).font(.system(size: 9, weight: .semibold)).foregroundStyle(Theme.accent) }
        }
        .padding(.vertical, 10)
        .frame(maxWidth: .infinity)
        .background(Theme.card, in: RoundedRectangle(cornerRadius: 10))
        .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(p.status == "error" ? Theme.red.opacity(0.5) : p.low ? Theme.accent.opacity(0.6) : Theme.border))
    }
}

// ── System ───────────────────────────────────────────────────────────
struct SystemView: View {
    @Environment(Store.self) private var store

    var body: some View {
        Page {
            if let n = store.now {
                let e = n.electrical
                Card(title: "Voltage") {
                    Row("System (L1+L2)", e.systemV.map { String(format: "%.1f V", $0) })
                    Row("L1", e.l1V.map { String(format: "%.1f V", $0) })
                    Row("L2", e.l2V.map { String(format: "%.1f V", $0) })
                    Row("Frequency", e.freqHz.map { String(format: "%.2f Hz", $0) })
                }
                Card(title: "Power factor") {
                    Row("Solar", e.pfSolar.map { String(format: "%.1f%%", $0 * 100) })
                    Row("Home", e.pfHome.map { String(format: "%.1f%%", $0 * 100) })
                }
                let s = n.system
                Card(title: "PVS") {
                    Row("Gateway", s.model)
                    Row("Serial", s.serial)
                    Row("Hardware", s.hw)
                    Row("Firmware", s.firmware)
                    Row("State", s.state)
                    Row("Memory", s.memKib.map { "\(Int($0)) KiB" })
                    Row("CT type", s.ctType)
                    Row("Lifetime solar", Fmt.kwh(n.lifetimeKwh))
                }
            } else {
                EmptyState()
            }
        }
    }
}

struct Row: View {
    let k: String
    let v: String?
    init(_ k: String, _ v: String?) { self.k = k; self.v = v }
    var body: some View {
        HStack {
            Text(k).font(.footnote).foregroundStyle(Theme.dim)
            Spacer()
            Text(v ?? "—").font(.footnote).monospacedDigit().textSelection(.enabled)
        }
    }
}
