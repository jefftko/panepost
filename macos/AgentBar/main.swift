// AgentBar — menu-bar front end for panepost's `ag` CLI. All data and actions go through ag;
// this app never parses tmux itself, so routing rules stay in one place.
import AppKit
import ServiceManagement
import SwiftUI
import UserNotifications

// `ag` is looked up in the usual install locations (apps launched from Finder get a minimal PATH).
let AG: String = {
    let cands = ["~/.local/bin/ag", "/opt/homebrew/bin/ag", "/usr/local/bin/ag"].map { NSString(string: $0).expandingTildeInPath }
    return cands.first { FileManager.default.isExecutableFile(atPath: $0) } ?? "ag"
}()

struct Agent: Decodable, Identifiable, Hashable {
    let pane_id: String
    let addr: String
    let session: String
    let window: Int
    let window_name: String
    let kind: String?
    let state: String?
    let name: String?
    let id: String?
    let id_source: String?
    let cwd: String
    let last_ms: Double?
    var ident: String { pane_id }
    var title: String { (name?.isEmpty == false ? name! : window_name) }
    var target: String {
        if let i = id, id_source == "hook" || id_source == "footer" || id_source == "claude-sessions" { return String(i.prefix(8)) }
        return pane_id
    }
    var sendLine: String { "ag send \(target) \"…\"   # \(kind ?? "shell") \(title) @ tmux \(addr)" }
    var resume: String? {
        guard let i = id else { return nil }
        return kind == "claude" ? "cd \(shq(cwd)) && claude --resume \(i)" : kind == "codex" ? "cd \(shq(cwd)) && codex resume \(i)" : nil
    }
}

struct Project: Decodable, Identifiable, Hashable {
    let dir: String
    let short: String
    let source: String
    let pinned: Bool?
    var id: String { dir }
}

func shq(_ s: String) -> String { "'" + s.replacingOccurrences(of: "'", with: "'\\''") + "'" }

func shortPath(_ p: String) -> String {
    let h = NSHomeDirectory()
    return p.hasPrefix(h) ? "~" + p.dropFirst(h.count) : p
}

func age(_ ms: Double?) -> String {
    guard let ms = ms, ms > 0 else { return "–" }
    let s = max(0, Date().timeIntervalSince1970 - ms / 1000)
    if s < 60 { return "\(Int(s))s" }
    if s < 3600 { return "\(Int(s / 60))m" }
    if s < 86400 { return "\(Int(s / 3600))h" }
    return "\(Int(s / 86400))d"
}

func runAg(_ args: [String], _ done: @escaping (Int32, String, String) -> Void) {
    DispatchQueue.global(qos: .userInitiated).async {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        p.arguments = [AG] + args
        var env = ProcessInfo.processInfo.environment
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:\(NSHomeDirectory())/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        p.environment = env
        let o = Pipe(), e = Pipe()
        p.standardOutput = o
        p.standardError = e
        var out = "", err = ""
        do {
            try p.run()
            let od = o.fileHandleForReading.readDataToEndOfFile()
            let ed = e.fileHandleForReading.readDataToEndOfFile()
            p.waitUntilExit()
            out = String(decoding: od, as: UTF8.self)
            err = String(decoding: ed, as: UTF8.self)
        } catch { err = "\(error)" }
        let rc = p.isRunning ? -1 : p.terminationStatus
        DispatchQueue.main.async { done(rc, out, err) }
    }
}

func runShell(_ script: String, _ done: @escaping (Int32, String) -> Void) {
    DispatchQueue.global(qos: .userInitiated).async {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/zsh")
        p.arguments = ["-c", script]
        var env = ProcessInfo.processInfo.environment
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        p.environment = env
        let o = Pipe()
        p.standardOutput = o
        p.standardError = o
        do { try p.run() } catch { DispatchQueue.main.async { done(-1, "\(error)") }; return }
        let d = o.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        DispatchQueue.main.async { done(p.terminationStatus, String(decoding: d, as: UTF8.self)) }
    }
}

func pbCopy(_ s: String) {
    NSPasteboard.general.clearContents()
    NSPasteboard.general.setString(s, forType: .string)
}

final class Store: NSObject, ObservableObject, UNUserNotificationCenterDelegate {
    @Published var agents: [Agent] = []
    @Published var projects: [Project] = []
    @Published var updated = "—"
    @Published var error = ""
    @Published var toast = ""
    @Published var plan = ""
    private var prev: [String: String] = [:]
    private var first = true
    private var timer: Timer?
    private var busyRefresh = false

    override init() {
        super.init()
        UNUserNotificationCenter.current().delegate = self
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { _, _ in }
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 20, repeats: true) { [weak self] _ in self?.refresh() }
    }

    func count(_ st: String) -> Int { agents.filter { $0.state == st }.count }
    var badge: String {
        let w = count("wait"), b = count("busy")
        return w > 0 ? "\(w) wait · \(b) busy" : "\(b) busy"
    }

    func refresh() {
        if busyRefresh { return }
        busyRefresh = true
        runAg(["ls", "--json"]) { rc, out, err in
            self.busyRefresh = false
            guard rc == 0, let d = out.data(using: .utf8), let a = try? JSONDecoder().decode([Agent].self, from: d) else {
                self.error = rc == 0 ? "could not parse ag output" : err.trimmingCharacters(in: .whitespacesAndNewlines)
                return
            }
            self.error = ""
            self.notifyTransitions(a)
            self.agents = a
            let f = DateFormatter()
            f.dateFormat = "HH:mm:ss"
            self.updated = f.string(from: Date())
        }
    }

    private func notifyTransitions(_ a: [Agent]) {
        var now: [String: String] = [:]
        for x in a {
            let st = x.state ?? "idle"
            now[x.pane_id] = st
            guard !first, let old = prev[x.pane_id], old != st else { continue }
            if st == "wait" { notify("\(x.title) is waiting for you", "\(x.kind ?? "") · \(x.addr) · \(shortPath(x.cwd))", x.pane_id) }
            else if old == "busy" && st == "idle" { notify("\(x.title) finished", "\(x.kind ?? "") · \(x.addr)", x.pane_id) }
        }
        prev = now
        first = false
    }

    private func notify(_ title: String, _ body: String, _ pane: String) {
        let c = UNMutableNotificationContent()
        c.title = title
        c.body = body
        c.sound = .default
        c.userInfo = ["pane": pane]
        UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: UUID().uuidString, content: c, trigger: nil))
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
                                withCompletionHandler done: @escaping () -> Void) {
        if let pane = response.notification.request.content.userInfo["pane"] as? String { go(pane) }
        done()
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent n: UNNotification,
                                withCompletionHandler done: @escaping (UNNotificationPresentationOptions) -> Void) {
        done([.banner, .sound])
    }

    func go(_ target: String) { runAg(["go", target]) { _, _, err in if !err.isEmpty { self.flash(err) } } }

    func flash(_ s: String) {
        toast = s
        DispatchQueue.main.asyncAfter(deadline: .now() + 2.2) { if self.toast == s { self.toast = "" } }
    }

    func copyFlash(_ s: String, _ what: String) { pbCopy(s); flash("Copied\(what): \(s.prefix(60))") }

    func loadProjects() {
        runAg(["projects", "--json"]) { rc, out, _ in
            if rc == 0, let d = out.data(using: .utf8), let p = try? JSONDecoder().decode([Project].self, from: d) {
                self.projects = p
            }
        }
    }

    func pin(_ p: Project) {
        runAg((p.pinned ?? false) ? ["pin", "--rm", p.dir] : ["pin", p.dir]) { _, _, _ in self.loadProjects() }
    }

    func create(dir: String, agent: String, session: String, name: String, jump: Bool) {
        var a = ["new", dir, "--agent", agent]
        if !session.isEmpty { a += ["--session", session] }
        if !name.isEmpty { a += ["--name", name] }
        if jump { a.append("--go") }
        runAg(a) { rc, out, err in
            self.flash(rc == 0 ? "Started: \(out.prefix(120))" : "Failed: \(err)")
            DispatchQueue.main.asyncAfter(deadline: .now() + 2) { self.refresh() }
        }
    }

    func loadPlan() { runAg(["restore", "--plan"]) { _, out, err in self.plan = out.isEmpty ? err : out } }

    func restore(_ session: String) {
        runAg(["restore", "--plan", "--session", session]) { rc, out, _ in
            let cmds = out.split(separator: "\n").map(String.init).filter { $0.hasPrefix("tmux ") }
            guard rc == 0, !cmds.isEmpty else { self.flash("No restore commands to run"); return }
            runShell(cmds.joined(separator: "\n")) { rc2, o2 in
                self.flash(rc2 == 0 ? "Restored \(session) (\(cmds.count) commands)" : "Partly failed: \(o2.prefix(160))")
                self.loadPlan()
                DispatchQueue.main.asyncAfter(deadline: .now() + 3) { self.refresh() }
            }
        }
    }
}

func color(_ st: String?) -> Color {
    switch st {
    case "wait": return .red
    case "busy": return .orange
    case "draft": return .yellow
    default: return .green
    }
}

func stateText(_ st: String?) -> String {
    switch st {
    case "wait": return "waiting"
    case "busy": return "busy"
    case "draft": return "draft"
    default: return "idle"
    }
}

struct AgentRow: View {
    @EnvironmentObject var s: Store
    let a: Agent
    @State private var hover = false
    var body: some View {
        HStack(spacing: 8) {
            Circle().fill(color(a.state)).frame(width: 9, height: 9)
            VStack(alignment: .leading, spacing: 1) {
                HStack(spacing: 6) {
                    Text(a.title).font(.system(size: 12.5, weight: .semibold)).lineLimit(1)
                    Text(stateText(a.state)).font(.system(size: 10)).foregroundColor(color(a.state))
                }
                Text("\(a.kind ?? "") · \(a.addr) · \(a.pane_id)\(a.id.map { " · " + $0.prefix(8) } ?? "")\(a.id_source == "ambiguous" || a.id_source == "unknown" ? "?" : "") · \(age(a.last_ms))")
                    .font(.system(size: 10.5, design: .monospaced)).foregroundColor(.secondary).lineLimit(1)
                Text(shortPath(a.cwd)).font(.system(size: 10)).foregroundColor(.secondary).lineLimit(1).truncationMode(.middle)
            }
            Spacer(minLength: 4)
            Menu {
                Button("Copy reply line") { s.copyFlash(a.sendLine, " reply line") }
                Button("Copy pane id (\(a.pane_id))") { s.copyFlash(a.pane_id, " pane id") }
                if let i = a.id { Button("Copy session id") { s.copyFlash(i, " session id") } }
                if let r = a.resume { Button("Copy resume command") { s.copyFlash(r, " resume command") } }
                Button("Copy tmux address (\(a.addr))") { s.copyFlash(a.addr, " address") }
                Button("Copy directory") { s.copyFlash(a.cwd, " directory") }
                Divider()
                Button("New Claude here") { s.create(dir: a.cwd, agent: "claude", session: a.session, name: "", jump: true) }
                Button("New Codex here") { s.create(dir: a.cwd, agent: "codex", session: a.session, name: "", jump: true) }
            } label: { Image(systemName: "doc.on.doc") }
                .menuStyle(.borderlessButton).frame(width: 26)
            Button { s.go(a.pane_id) } label: { Image(systemName: "arrow.up.forward.app") }
                .buttonStyle(.borderless).help("Jump to this pane")
        }
        .padding(.vertical, 4).padding(.horizontal, 8)
        .background(RoundedRectangle(cornerRadius: 6).fill(hover ? Color.primary.opacity(0.07) : .clear))
        .contentShape(Rectangle())
        .onHover { hover = $0 }
        .onTapGesture { s.copyFlash(a.sendLine, " reply line") }
        .help("Click: copy reply line · buttons: more copies / jump")
    }
}

struct AgentsTab: View {
    @EnvironmentObject var s: Store
    @State private var q = ""
    @State private var only = "all"
    var filtered: [Agent] {
        s.agents.filter { a in
            (only == "all" || a.state == only) &&
                (q.isEmpty || "\(a.title) \(a.addr) \(a.cwd) \(a.kind ?? "") \(a.id ?? "") \(a.pane_id)".localizedCaseInsensitiveContains(q))
        }
    }
    var body: some View {
        VStack(spacing: 6) {
            HStack {
                TextField("Search name / dir / pane / id", text: $q).textFieldStyle(.roundedBorder)
                Picker("", selection: $only) {
                    Text("All").tag("all"); Text("Waiting \(s.count("wait"))").tag("wait"); Text("Busy \(s.count("busy"))").tag("busy")
                    Text("Draft \(s.count("draft"))").tag("draft"); Text("Idle \(s.count("idle"))").tag("idle")
                }.frame(width: 120)
            }
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 2) {
                    let groups = Dictionary(grouping: filtered, by: { $0.session })
                    ForEach(groups.keys.sorted(), id: \.self) { k in
                        Text("\(k)  ·  \(groups[k]!.count)").font(.system(size: 11, weight: .bold)).foregroundColor(.secondary)
                            .padding(.top, 6).padding(.leading, 4)
                        ForEach(groups[k]!.sorted { ($0.window, $0.addr) < ($1.window, $1.addr) }) { a in AgentRow(a: a) }
                    }
                    if filtered.isEmpty { Text(s.error.isEmpty ? "No matching agents" : s.error).foregroundColor(.secondary).padding() }
                }
            }
        }
    }
}

struct NewTab: View {
    @EnvironmentObject var s: Store
    @State private var dir = ""
    @State private var agent = "claude"
    @State private var session = ""
    @State private var name = ""
    @State private var jump = true
    @State private var q = ""
    var sessions: [String] { Array(Set(s.agents.map { $0.session })).sorted() }
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Picker("", selection: $agent) { Text("Claude").tag("claude"); Text("Codex").tag("codex"); Text("Shell").tag("shell") }
                    .pickerStyle(.segmented).frame(width: 220)
                Spacer()
                Button("Choose folder…") {
                    let p = NSOpenPanel()
                    p.canChooseDirectories = true; p.canChooseFiles = false; p.allowsMultipleSelection = false
                    NSApp.activate(ignoringOtherApps: true)
                    if p.runModal() == .OK, let u = p.url { dir = u.path }
                }
            }
            TextField("Directory (pick below or paste a path)", text: $dir).textFieldStyle(.roundedBorder)
            HStack {
                TextField("tmux session (empty = reuse this dir's session, else dir name)", text: $session).textFieldStyle(.roundedBorder)
                Menu("Existing") { ForEach(sessions, id: \.self) { x in Button(x) { session = x } } }.frame(width: 64)
            }
            HStack {
                TextField("Window name (empty = dir name)", text: $name).textFieldStyle(.roundedBorder)
                Toggle("Jump after start", isOn: $jump)
                Button("Start") { s.create(dir: dir, agent: agent, session: session, name: name, jump: jump) }
                    .keyboardShortcut(.defaultAction).disabled(dir.isEmpty)
            }
            Divider()
            TextField("Filter directories", text: $q).textFieldStyle(.roundedBorder)
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 1) {
                    ForEach(s.projects.filter { q.isEmpty || $0.dir.localizedCaseInsensitiveContains(q) }) { p in
                        HStack {
                            Button { s.pin(p) } label: { Image(systemName: (p.pinned ?? false) ? "star.fill" : "star") }
                                .buttonStyle(.borderless)
                            Text(p.short).font(.system(size: 11.5)).lineLimit(1).truncationMode(.middle)
                            Spacer()
                            Text(p.source).font(.system(size: 10)).foregroundColor(.secondary)
                        }
                        .padding(.vertical, 3).padding(.horizontal, 6)
                        .background(RoundedRectangle(cornerRadius: 5).fill(dir == p.dir ? Color.accentColor.opacity(0.18) : .clear))
                        .contentShape(Rectangle())
                        .onTapGesture { dir = p.dir }
                    }
                }
            }
        }
        .onAppear { s.loadProjects() }
    }
}

struct RestoreTab: View {
    @EnvironmentObject var s: Store
    @State private var confirm: String? = nil
    var sections: [(String, Bool)] {
        s.plan.split(separator: "\n").compactMap { l -> (String, Bool)? in
            guard l.hasPrefix("# ---- ") else { return nil }
            let rest = l.dropFirst(7)
            let skipped = rest.contains("already running")
            return (String(rest.split(separator: " (").first ?? rest).trimmingCharacters(in: .whitespaces), skipped)
        }
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(s.plan.split(separator: "\n").first.map(String.init) ?? "Reading snapshot…")
                .font(.system(size: 10.5)).foregroundColor(.secondary).lineLimit(2)
            let missing = sections.filter { !$0.1 }
            if missing.isEmpty {
                Text("Every session in the snapshot is running. After a reboot, missing sessions show up here.").foregroundColor(.secondary)
            }
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 4) {
                    ForEach(missing, id: \.0) { sec in
                        HStack {
                            Text(sec.0).font(.system(size: 12.5, weight: .semibold))
                            Spacer()
                            Button("Copy commands") {
                                runAg(["restore", "--plan", "--session", sec.0]) { _, out, _ in s.copyFlash(out, " resume command") }
                            }
                            Button("Restore") { confirm = sec.0 }
                        }.padding(.horizontal, 6)
                    }
                }
            }
            HStack {
                Button("Refresh plan") { s.loadPlan() }
                Button("Copy full plan") { s.copyFlash(s.plan, " full plan") }
                Spacer()
            }
        }
        .onAppear { s.loadPlan() }
        .confirmationDialog("Restore tmux session \"\(confirm ?? "")\"?", isPresented: Binding(get: { confirm != nil }, set: { if !$0 { confirm = nil } })) {
            Button("Restore (rebuild windows and resume)") { if let c = confirm { s.restore(c) }; confirm = nil }
            Button("Cancel", role: .cancel) { confirm = nil }
        } message: { Text("Rebuilds windows and layout from the snapshot and runs claude --resume / codex resume in each pane.") }
    }
}

struct Panel: View {
    @EnvironmentObject var s: Store
    @State private var tab = 0
    @State private var login = SMAppService.mainApp.status == .enabled
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 10) {
                Text("Agents \(s.agents.count)").font(.system(size: 13, weight: .bold))
                Label("\(s.count("wait"))", systemImage: "hand.raised.fill").foregroundColor(.red)
                Label("\(s.count("busy"))", systemImage: "bolt.fill").foregroundColor(.orange)
                Label("\(s.count("draft"))", systemImage: "pencil").foregroundColor(.yellow)
                Label("\(s.count("idle"))", systemImage: "checkmark.circle").foregroundColor(.green)
                Spacer()
                Text(s.updated).font(.system(size: 10)).foregroundColor(.secondary)
                Button { s.refresh() } label: { Image(systemName: "arrow.clockwise") }.buttonStyle(.borderless)
            }.font(.system(size: 11.5))
            Picker("", selection: $tab) { Text("Agent").tag(0); Text("New").tag(1); Text("Restore").tag(2) }.pickerStyle(.segmented)
            Group {
                if tab == 0 { AgentsTab() } else if tab == 1 { NewTab() } else { RestoreTab() }
            }.frame(maxHeight: .infinity, alignment: .top)
            HStack {
                Text(s.toast).font(.system(size: 10.5)).foregroundColor(.accentColor).lineLimit(1)
                Spacer()
                Toggle("Launch at login", isOn: $login).toggleStyle(.checkbox).font(.system(size: 10.5))
                    .onChange(of: login) { _, v in
                        do { if v { try SMAppService.mainApp.register() } else { try SMAppService.mainApp.unregister() } }
                        catch { s.flash("Login item failed: \(error.localizedDescription)"); login = !v }
                    }
                Button("Quit") { NSApp.terminate(nil) }.buttonStyle(.borderless).font(.system(size: 10.5))
            }
        }
        .padding(12)
        .frame(width: 560, height: 640)
        .onAppear { s.refresh() }
    }
}

@main
struct AgentBarApp: App {
    @StateObject private var store = Store()
    var body: some Scene {
        MenuBarExtra {
            Panel().environmentObject(store)
        } label: {
            HStack(spacing: 3) {
                Image(systemName: store.count("wait") > 0 ? "person.fill.questionmark" : "person.3.sequence.fill")
                Text(store.badge)
            }
        }
        .menuBarExtraStyle(.window)
    }
}
