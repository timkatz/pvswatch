import SwiftUI

// Server address (stored on this device) + the server's own settings
// (/api/v1/settings, shared by every client).
struct SettingsView: View {
    @Environment(Store.self) private var store
    @State private var serverDraft = ""
    @State private var drafts: [String: String] = [:]
    @State private var message: String?
    @State private var saving = false

    var body: some View {
        Form {
            Section {
                TextField("https://pvswatch.your-tailnet.ts.net", text: $serverDraft)
                    .textContentType(.URL)
                    #if os(iOS)
                    .keyboardType(.URL)
                    .textInputAutocapitalization(.never)
                    #endif
                    .autocorrectionDisabled()
                    .onSubmit(connect)
                HStack {
                    Button("Connect", action: connect)
                        .disabled(serverDraft.trimmingCharacters(in: .whitespaces) == store.server)
                    Spacer()
                    if let e = store.error { Text(e).font(.caption).foregroundStyle(Theme.red).lineLimit(2) }
                    else if store.lastOK != nil { Label("Connected", systemImage: "checkmark.circle.fill").font(.caption).foregroundStyle(Theme.green) }
                }
            } header: {
                Text("Server")
            } footer: {
                Text("Your pvswatch address. Reach it over Tailscale; pvswatch has no login of its own.")
            }

            if let d = store.settings {
                Section("Battery mode") { BatteryModePicker().listRowInsets(EdgeInsets()) }

                ForEach(groups(d), id: \.0) { name, items in
                    Section(name) {
                        ForEach(items) { item in field(item) }
                    }
                }

                Section {
                    Button(saving ? "Saving…" : "Save changes", action: save).disabled(saving || changes(d).isEmpty)
                    if let message { Text(message).font(.caption).foregroundStyle(message == "Saved" ? Theme.green : Theme.red) }
                } footer: {
                    Text("Passwords and tokens stay in the server's .env.")
                }

                Section("On the server") {
                    let s = d.status
                    Row("PVS connection", s.mock ? "mock data" : s.pvsConfigured ? "on" : "off")
                    Row("Alerts (ntfy)", s.alerts ? (s.alertsFallback ? "on + fallback" : "on") : "off")
                    Row("Grid outage tracking", s.upsTracking ? "on" : "off")
                    Row("Utility bill sync", s.utilityApi ? "on" : "off")
                    Row("PVS refresh", "every \(s.refreshSecs / 60) min")
                }
            }

            Section("About") {
                Row("Version", Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String)
                Row("Build", Bundle.main.infoDictionary?["PvsCommit"] as? String)
            }
        }
        .formStyle(.grouped)
        .scrollContentBackground(.hidden)
        .background(Theme.bg)
        .onAppear { serverDraft = store.server }
        .task(id: store.server) { await store.loadSettings(); resetDrafts() }
    }

    private func connect() {
        var s = serverDraft.trimmingCharacters(in: .whitespaces)
        if !s.isEmpty, !s.contains("://") { s = "https://" + s }
        serverDraft = s
        store.server = s
    }

    private func groups(_ d: SettingsDoc) -> [(String, [SettingsDoc.Item])] {
        var out: [(String, [SettingsDoc.Item])] = []
        for i in d.settings {
            if let k = out.firstIndex(where: { $0.0 == i.group }) { out[k].1.append(i) } else { out.append((i.group, [i])) }
        }
        return out
    }

    @ViewBuilder private func field(_ item: SettingsDoc.Item) -> some View {
        let binding = Binding(get: { drafts[item.key] ?? item.value.text }, set: { drafts[item.key] = $0 })
        if item.type == "json" {
            VStack(alignment: .leading) {
                Text(item.label)
                TextEditor(text: binding).font(.system(.caption, design: .monospaced)).frame(minHeight: 120)
                if item.overridden { Button("Reset to default") { resetKey(item.key) }.font(.caption) }
            }
        } else {
            HStack {
                VStack(alignment: .leading) {
                    Text(item.label)
                    if item.overridden {
                        Button("Default \(item.default.text.isEmpty ? "blank" : item.default.text) · reset") { resetKey(item.key) }
                            .font(.caption).buttonStyle(.borderless)
                    }
                }
                Spacer()
                TextField("", text: binding)
                    .multilineTextAlignment(.trailing)
                    .frame(width: 90)
                    #if os(iOS)
                    .keyboardType(item.type == "text" ? .numbersAndPunctuation : .decimalPad)
                    #endif
                if let u = item.unit { Text(u).font(.caption).foregroundStyle(Theme.dim).frame(minWidth: 34, alignment: .leading) }
            }
        }
    }

    private func changes(_ d: SettingsDoc) -> [String: Any?] {
        var out: [String: Any?] = [:]
        for i in d.settings {
            guard let v = drafts[i.key]?.trimmingCharacters(in: .whitespacesAndNewlines), v != i.value.text else { continue }
            // updateValue, not subscript: assigning nil through the
            // subscript would drop the key instead of sending null.
            let value: Any? = (i.type == "int" || i.type == "number") ? Double(v) : v
            out.updateValue(value, forKey: i.key)
        }
        return out
    }

    private func save() {
        guard let d = store.settings else { return }
        let c = changes(d)
        saving = true
        Task {
            let err = await store.saveSettings(c)
            saving = false
            message = err ?? "Saved"
            if err == nil { resetDrafts() }
        }
    }

    private func resetKey(_ key: String) {
        Task {
            let err = await store.saveSettings([key: nil])
            message = err ?? "Saved"
            if err == nil { drafts[key] = nil }
        }
    }

    private func resetDrafts() { drafts = [:] }
}
