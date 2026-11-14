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
        TileGrid {
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
    @State private var sel: Date?

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
            if let x = nearest(r, to: sel, by: \.date) {
                let out = outages.first { $0.start <= x.t && x.t <= ($0.end ?? .infinity) }
                RuleMark(x: .value("t", x.date)).foregroundStyle(Theme.dim.opacity(0.6))
                    .annotation(position: .top, spacing: 0, overflowResolution: .init(x: .fit(to: .chart), y: .fit(to: .chart))) {
                        ChartTip(title: x.date.tipLabel, rows: [
                            ("Solar", kwText(x.productionKw), Theme.solar),
                            ("Home", kwText(x.consumptionKw), Theme.home),
                            ("Battery", flowText(x.batteryKw, plus: "discharging", minus: "charging"), Theme.cyan),
                            ("Grid", flowText(x.netKw, plus: "importing", minus: "exporting"), Theme.green),
                        ] + (out.map { [($0.kind == "grid" ? "Power outage" : "PVS offline", Fmt.duration($0.durationS), $0.kind == "grid" ? Theme.red : Theme.dim)] } ?? []))
                    }
            }
        }
        .chartXSelection(value: $sel)
        .chartForegroundStyleScale(["Solar": Theme.solar, "Home": Theme.home, "Battery": Theme.cyan, "Grid": Theme.green])
        .chartLegend(position: .top)
        // `-tipDemo YES` launch argument (development): pin a tooltip open.
        .onAppear { if UserDefaults.standard.bool(forKey: "tipDemo"), !r.isEmpty { sel = r[r.count * 3 / 4].date } }
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
    @State private var sel: Date?
    var body: some View {
        let pts = history.readings.filter { $0.batterySoc != nil }
        Chart {
            ForEach(pts) { x in
                AreaMark(x: .value("t", x.date), y: .value("%", (x.batterySoc ?? 0) * 100))
                    .foregroundStyle(Theme.green.opacity(0.25))
                LineMark(x: .value("t", x.date), y: .value("%", (x.batterySoc ?? 0) * 100))
                    .foregroundStyle(Theme.green)
            }
            if let x = nearest(pts, to: sel, by: \.date) {
                RuleMark(x: .value("t", x.date)).foregroundStyle(Theme.dim.opacity(0.6))
                    .annotation(position: .top, spacing: 0, overflowResolution: .init(x: .fit(to: .chart), y: .fit(to: .chart))) {
                        ChartTip(title: x.date.tipLabel, rows: [("Charge", Fmt.pct(x.batterySoc), Theme.green),
                                                                 ("Battery", flowText(x.batteryKw, plus: "discharging", minus: "charging"), Theme.cyan)])
                    }
            }
        }
        .chartXSelection(value: $sel)
        .chartYScale(domain: 0...100)
        .chartXAxis {
            AxisMarks(values: .automatic(desiredCount: 5)) { _ in
                AxisGridLine()
                AxisValueLabel(format: (pts.last?.t ?? 0) - (pts.first?.t ?? 0) > 2 * 86400
                    ? .dateTime.month(.abbreviated).day() : .dateTime.hour())
            }
        }
    }
}

// ── Money ────────────────────────────────────────────────────────────
struct MoneyView: View {
    @Environment(Store.self) private var store

    var body: some View {
        Page {
            if let f = store.finance {
                if let y = f.headline?.ytd {
                    SectionHead(title: "\(y.year) year to date", trailing: "\(y.bills) bills")
                    YtdTiles(y: y)
                }
                SectionHead(title: "Year over year", trailing: "each bill vs the same month a year earlier")
                Card { MonthlyYoY(rows: Array(f.rows.suffix(12))).frame(height: 220) }
                if let years = store.financeYears?.rows, years.count > 1 {
                    Card(title: "By year") { YearBars(rows: years).frame(height: 200) }
                }
                SectionHead(title: "Since solar")
                if let h = f.headline { FinanceTiles(h: h) }
                Card(title: "By bill") {
                    HStack {
                        Text("Bill").frame(maxWidth: .infinity, alignment: .leading)
                        Text("Paid").frame(width: 70, alignment: .trailing)
                        Text("vs last year").frame(width: 100, alignment: .trailing)
                        Text("Solar").frame(width: 100, alignment: .trailing)
                    }
                    .font(.caption2.weight(.semibold)).foregroundStyle(Theme.dim)
                    ForEach(f.rows.suffix(12).reversed()) { r in
                        HStack {
                            HStack(spacing: 4) {
                                Text(monthLabel(r.key)).font(.footnote)
                                if r.inProgress == true { Text("so far").font(.caption2).foregroundStyle(Theme.dim) }
                            }
                            .frame(maxWidth: .infinity, alignment: .leading)
                            Text(Fmt.money(r.total)).font(.footnote).monospacedDigit().frame(width: 70, alignment: .trailing)
                            Delta(now: r.total, before: r.yoyTotal).frame(width: 100, alignment: .trailing)
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
            if let tou = store.tou, tou.enabled {
                SectionHead(title: "Rates")
                TouCard(tou: tou)
            }
        }
        .refreshable { await store.loadMoney() }
    }
}

func monthLabel(_ key: String) -> String {
    let p = key.split(separator: "-").compactMap { Int($0) }
    guard p.count == 2, let d = Calendar.current.date(from: DateComponents(year: p[0], month: p[1])) else { return key }
    return d.formatted(.dateTime.month(.abbreviated).year())
}

private func shortMonth(_ key: String) -> String {
    let p = key.split(separator: "-").compactMap { Int($0) }
    guard p.count == 2, let d = Calendar.current.date(from: DateComponents(year: p[0], month: p[1])) else { return key }
    return d.formatted(.dateTime.month(.abbreviated))
}

// "+$198 (+28%)" in red when you paid more than a year ago, green when less.
private struct Delta: View {
    let now: Double?
    let before: Double?
    var body: some View {
        if let now, let before, before > 0 {
            let d = now - before
            Text("\(d >= 0 ? "+" : "−")\(Fmt.money(abs(d))) (\(d >= 0 ? "+" : "−")\(Int((abs(d) / before * 100).rounded()))%)")
                .font(.caption).monospacedDigit()
                .foregroundStyle(d > 0 ? Theme.red : Theme.green)
        } else {
            Text("—").font(.caption).foregroundStyle(Theme.dim)
        }
    }
}

private struct YtdTiles: View {
    let y: Finance.Ytd
    var body: some View {
        let prevYear = y.prev?.year ?? (y.year - 1)
        TileGrid(wide: 3) {
            StatTile(label: "Paid so far", value: Fmt.money(y.paid)) {
                HStack(spacing: 4) {
                    Text("vs \(Fmt.money(y.prev?.paid)) by now in \(String(prevYear))").font(.caption).foregroundStyle(Theme.dim)
                    Delta(now: y.paid, before: y.prev?.paid)
                }
            }
            StatTile(label: "Saved by solar", value: Fmt.money(y.saved),
                     sub: "vs \(Fmt.money(y.prev?.saved)) by now in \(String(prevYear))",
                     valueColor: (y.saved ?? 0) >= 0 ? Theme.green : Theme.red)
            StatTile(label: "Without solar", value: Fmt.money(y.noSolar),
                     sub: "estimated utility bill · \(Fmt.money(y.prev?.noSolar)) in \(String(prevYear))")
        }
    }
}

// The last 12 bills as bars, with the same month a year earlier as a dot.
private struct MonthlyYoY: View {
    let rows: [Finance.Row]
    @State private var sel: String?
    var body: some View {
        Chart {
            if let sel, let r = rows.first(where: { shortMonth($0.key) == sel }) {
                RuleMark(x: .value("Month", sel)).foregroundStyle(Theme.dim.opacity(0.25)).lineStyle(StrokeStyle(lineWidth: 28))
                    .annotation(position: .top, spacing: 0, overflowResolution: .init(x: .fit(to: .chart), y: .fit(to: .chart))) {
                        ChartTip(title: monthLabel(r.key), rows: [
                            ("Paid", Fmt.money(r.total), Theme.accent),
                            ("Year before", Fmt.money(r.yoyTotal), Theme.dim),
                            ("Change", changeText(r.total, r.yoyTotal), (r.total ?? 0) > (r.yoyTotal ?? 0) ? Theme.red : Theme.green),
                            ("Solar", r.saved.map { ($0 >= 0 ? "saved " : "lost ") + Fmt.money(abs($0)) } ?? "—", (r.saved ?? 0) >= 0 ? Theme.green : Theme.red),
                        ])
                    }
            }
            ForEach(rows) { r in
                BarMark(x: .value("Month", shortMonth(r.key)), y: .value("$", r.total ?? 0))
                    .foregroundStyle(by: .value("Series", "This year"))
                    .cornerRadius(3)
                if let y = r.yoyTotal {
                    PointMark(x: .value("Month", shortMonth(r.key)), y: .value("$", y))
                        .foregroundStyle(by: .value("Series", "Year before"))
                        .symbolSize(60)
                }
            }
        }
        .chartXSelection(value: $sel)
        .chartForegroundStyleScale(["This year": Theme.accent, "Year before": Theme.dim])
        .chartLegend(position: .top, alignment: .leading)
        .chartYAxis { AxisMarks { v in AxisGridLine(); AxisValueLabel { if let d = v.as(Double.self) { Text(Fmt.money(d)) } } } }
    }
}

// Each calendar year: what you paid vs the no-solar estimate.
private struct YearBars: View {
    let rows: [Finance.Row]
    @State private var sel: String?
    private func label(_ r: Finance.Row) -> String { r.key + (r.inProgress == true ? " (so far)" : "") }
    var body: some View {
        Chart {
            if let sel, let r = rows.first(where: { label($0) == sel }) {
                RuleMark(x: .value("Year", sel)).foregroundStyle(Theme.dim.opacity(0.18)).lineStyle(StrokeStyle(lineWidth: 90))
                    .annotation(position: .top, spacing: 0, overflowResolution: .init(x: .fit(to: .chart), y: .fit(to: .chart))) {
                        ChartTip(title: label(r), rows: [
                            ("Paid", Fmt.money(r.total), Theme.accent),
                            ("Without solar", Fmt.money(r.noSolar), Theme.dim),
                            ("Solar", r.saved.map { ($0 >= 0 ? "saved " : "lost ") + Fmt.money(abs($0)) } ?? "—", (r.saved ?? 0) >= 0 ? Theme.green : Theme.red),
                            ("vs year before", changeText(r.total, r.yoyTotal), (r.total ?? 0) > (r.yoyTotal ?? 0) ? Theme.red : Theme.green),
                        ])
                    }
            }
            ForEach(rows) { r in
                BarMark(x: .value("Year", r.key + (r.inProgress == true ? " (so far)" : "")), y: .value("$", r.total ?? 0))
                    .foregroundStyle(by: .value("Series", "Paid"))
                    .position(by: .value("Series", "Paid"))
                if let n = r.noSolar {
                    BarMark(x: .value("Year", r.key + (r.inProgress == true ? " (so far)" : "")), y: .value("$", n))
                        .foregroundStyle(by: .value("Series", "Without solar"))
                        .position(by: .value("Series", "Without solar"))
                }
            }
        }
        .chartXSelection(value: $sel)
        .chartForegroundStyleScale(["Paid": Theme.accent, "Without solar": Theme.dim.opacity(0.6)])
        .chartLegend(position: .top, alignment: .leading)
        .chartYAxis { AxisMarks { v in AxisGridLine(); AxisValueLabel { if let d = v.as(Double.self) { Text(Fmt.money(d)) } } } }
    }
}

private struct FinanceTiles: View {
    let h: Finance.Headline
    var body: some View {
        TileGrid(wide: 2) {
            if let l = h.lifetime {
                MoneyTile(label: "Saved since \(l.since?.prefix(7) ?? "start")", value: l.saved,
                          sub: "paid \(Fmt.money(l.paid)) vs ~\(Fmt.money(l.noSolar)) without solar")
            }
            if let c = h.netCost12, let n = h.bills12, n > 0 {
                StatTile(label: "Paid, last \(n) bills", value: Fmt.money(c), sub: "\(Fmt.money(c / Double(n)))/mo, utility + solar")
            }
        }
    }
}

private struct MoneyTile: View {
    let label: String
    let value: Double?
    let sub: String
    var body: some View {
        StatTile(label: label, value: Fmt.money(value), sub: sub,
                 valueColor: (value ?? 0) >= 0 ? Theme.green : Theme.red)
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
                TileGrid(wide: 2) {
                    StatTile(label: "Power outages · 30d", value: "\(o.summary.grid.count)",
                             sub: Fmt.duration(o.summary.grid.totalS) + " total")
                    StatTile(label: "PVS offline · 30d", value: "\(o.summary.pvs.count)",
                             sub: Fmt.duration(o.summary.pvs.totalS) + " total")
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
    @State private var selected: Now.Panel?

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
                    ForEach(p.items) { item in
                        Button { selected = item } label: { PanelCell(p: item) }.buttonStyle(.plain)
                    }
                }
            } else {
                EmptyState()
            }
        }
        .appModal(item: $selected) { PanelDetail(panel: $0) { selected = nil } }
        // `-panel <serial>` launch argument (development) opens one panel.
        .onChange(of: store.now?.panels.count, initial: true) { _, _ in
            if selected == nil, let s = UserDefaults.standard.string(forKey: "panel"),
               let item = store.now?.panels.items.first(where: { $0.serial == s }) { selected = item }
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


// Tooltip text helpers.
func kwText(_ v: Double?) -> String { v.map { String(format: "%.2f kW", $0) } ?? "—" }
func flowText(_ v: Double?, plus: String, minus: String) -> String {
    guard let v else { return "—" }
    if abs(v) < 0.02 { return "idle" }
    return String(format: "%.2f kW ", abs(v)) + (v > 0 ? plus : minus)
}
func changeText(_ now: Double?, _ before: Double?) -> String {
    guard let now, let before, before > 0 else { return "—" }
    let d = now - before
    return "\(d >= 0 ? "+" : "−")\(Fmt.money(abs(d))) (\(d >= 0 ? "+" : "−")\(Int((abs(d) / before * 100).rounded()))%)"
}
