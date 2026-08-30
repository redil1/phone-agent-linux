import Cocoa
import WebKit

private let studioURL = URL(string: "http://127.0.0.1:8090/")!
private let launchAgentLabel = "com.phoneagent.studio"

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate {
    private var window: NSWindow!
    private var webView: WKWebView!
    private var retryTimer: Timer?

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        installMenu()
        startStudioService()

        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .default()
        configuration.preferences.isTextInteractionEnabled = true
        webView = WKWebView(frame: .zero, configuration: configuration)
        webView.navigationDelegate = self

        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1440, height: 900),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = "PhoneAgent Studio"
        window.minSize = NSSize(width: 1040, height: 700)
        window.contentView = webView
        window.center()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        loadStudio()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }

    private func installMenu() {
        let menu = NSMenu()
        let appItem = NSMenuItem()
        menu.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(
            withTitle: "Reload Studio",
            action: #selector(reloadStudio),
            keyEquivalent: "r"
        )
        appMenu.addItem(NSMenuItem.separator())
        appMenu.addItem(
            withTitle: "Quit PhoneAgent",
            action: #selector(NSApplication.terminate(_:)),
            keyEquivalent: "q"
        )
        appItem.submenu = appMenu
        NSApp.mainMenu = menu
    }

    @objc private func reloadStudio() {
        startStudioService()
        loadStudio()
    }

    private func loadStudio() {
        retryTimer?.invalidate()
        webView.load(URLRequest(url: studioURL, cachePolicy: .reloadIgnoringLocalCacheData))
    }

    private func startStudioService() {
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        task.arguments = [
            "kickstart", "-k", "gui/\(getuid())/\(launchAgentLabel)"
        ]
        task.standardOutput = FileHandle.nullDevice
        task.standardError = FileHandle.nullDevice
        try? task.run()
    }

    func webView(
        _ webView: WKWebView,
        decidePolicyFor navigationAction: WKNavigationAction,
        decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
    ) {
        guard let url = navigationAction.request.url else {
            decisionHandler(.cancel)
            return
        }
        if url.host == "127.0.0.1" || url.host == "localhost" {
            decisionHandler(.allow)
            return
        }
        if navigationAction.navigationType == .linkActivated,
           url.scheme == "https" {
            NSWorkspace.shared.open(url)
        }
        decisionHandler(.cancel)
    }

    func webView(
        _ webView: WKWebView,
        didFailProvisionalNavigation navigation: WKNavigation!,
        withError error: Error
    ) {
        showStartingPage()
    }

    private func showStartingPage() {
        let html = """
        <!doctype html><meta charset="utf-8">
        <style>
        body{margin:0;background:#07090e;color:#f1f5f9;font:16px -apple-system;
        display:grid;place-items:center;height:100vh}main{text-align:center;max-width:520px}
        small{color:#94a3b8}button{margin-top:20px;padding:10px 18px;border:0;border-radius:8px}
        </style><main><h1>Starting PhoneAgent Studio…</h1>
        <small>The secured local service is not ready yet. This window will reconnect.</small></main>
        """
        webView.loadHTMLString(html, baseURL: nil)
        retryTimer?.invalidate()
        retryTimer = Timer.scheduledTimer(withTimeInterval: 1.5, repeats: false) {
            [weak self] _ in self?.loadStudio()
        }
    }
}

let application = NSApplication.shared
let delegate = AppDelegate()
application.delegate = delegate
application.run()
