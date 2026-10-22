import Foundation

/// 单次网络请求独占串行委托队列，容量检查先于写入，取消等待连接和文件句柄结算
final class UpdateTransfer: NSObject, URLSessionDataDelegate, @unchecked Sendable {
    private let queue: OperationQueue = {
        let value = OperationQueue(); value.maxConcurrentOperationCount = 1; return value
    }()
    private var session: URLSession?
    private var task: URLSessionDataTask?
    private var completion: CheckedContinuation<Data, Error>?
    private var output: FileHandle?
    private var content = Data()
    private var received: Int64 = 0
    private var limit: Int64 = 0
    private var failure: Error?
    private var cancelled = false
    private var progress: (@Sendable (Int64) -> Void)?

    func fetch(_ url: URL, limit: Int64, destination: URL? = nil, progress: (@Sendable (Int64) -> Void)? = nil) async throws -> Data {
        try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { continuation in
                queue.addOperation { [self] in
                    guard !cancelled else { continuation.resume(throwing: CancellationError()); return }
                    guard session == nil, Self.permitted(url), limit > 0 else { continuation.resume(throwing: UpdateError.unavailable); return }
                    completion = continuation; self.limit = limit; self.progress = progress
                    do {
                        if let destination {
                            let descriptor = open(destination.path, O_CREAT | O_EXCL | O_WRONLY | O_NOFOLLOW, 0o600)
                            guard descriptor >= 0 else { throw UpdateError.invalidLocation }
                            output = FileHandle(fileDescriptor: descriptor, closeOnDealloc: true)
                        }
                        let configuration = URLSessionConfiguration.ephemeral
                        configuration.httpCookieStorage = nil; configuration.httpShouldSetCookies = false
                        configuration.urlCache = nil; configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
                        configuration.timeoutIntervalForRequest = 30; configuration.timeoutIntervalForResource = 900
                        let session = URLSession(configuration: configuration, delegate: self, delegateQueue: queue)
                        self.session = session
                        var request = URLRequest(url: url)
                        request.setValue("TinkerFin-Desktop", forHTTPHeaderField: "User-Agent")
                        task = session.dataTask(with: request); task?.resume()
                    } catch { finish(error) }
                }
            }
        } onCancel: {
            self.queue.addOperation { [self] in cancelled = true; task?.cancel() }
        }
    }

    static func permitted(_ url: URL) -> Bool {
        url.scheme == "https" && url.user == nil && url.password == nil && (url.port == nil || url.port == 443)
            && ["api.github.com", "github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"].contains(url.host ?? "")
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping @Sendable (URLRequest?) -> Void) {
        completionHandler(request.url.map(Self.permitted) == true ? request : nil)
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive response: URLResponse,
                    completionHandler: @escaping @Sendable (URLSession.ResponseDisposition) -> Void) {
        guard let response = response as? HTTPURLResponse, response.statusCode == 200,
              response.expectedContentLength <= limit else {
            failure = UpdateError.unavailable; completionHandler(.cancel); return
        }
        completionHandler(.allow)
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        guard failure == nil else { return }
        do {
            received += Int64(data.count)
            guard received <= limit else { throw UpdateError.invalidArchive }
            if let output { try output.write(contentsOf: data) } else { content.append(data) }
            progress?(received)
        } catch { failure = error; task?.cancel() }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) { finish(failure ?? error) }

    private func finish(_ error: Error?) {
        do { try output?.close() } catch { if failure == nil { failure = error } }
        output = nil; progress = nil
        let result = completion; completion = nil
        session?.invalidateAndCancel(); session = nil; task = nil
        if cancelled { result?.resume(throwing: CancellationError()) }
        else if let error = failure ?? error { result?.resume(throwing: error) }
        else { result?.resume(returning: content) }
        content = Data()
    }
}

struct SignedDesktopRelease: Sendable {
    let release: DesktopRelease
    let manifest: Data
    let signature: Data
}

enum DesktopReleaseService {
    private struct PublishedRelease: Decodable {
        let tagName: String
        let draft: Bool
        let prerelease: Bool
        enum CodingKeys: String, CodingKey { case tagName = "tag_name", draft, prerelease }
    }

    static func check(current: String) async throws -> SignedDesktopRelease? {
        let current = try DesktopReleaseVersion(current)
        let url = URL(string: "https://api.github.com/repos/\(DesktopRelease.repository)/releases?per_page=100")!
        let bytes = try await UpdateTransfer().fetch(url, limit: 4 * 1024 * 1024)
        let published = try JSONDecoder().decode([PublishedRelease].self, from: bytes)
        let releases = published.filter { !$0.draft && !$0.prerelease && $0.tagName.hasPrefix("desktop-v") }
            .compactMap { value -> (String, DesktopReleaseVersion)? in
                let number = String(value.tagName.dropFirst("desktop-v".count))
                guard let version = try? DesktopReleaseVersion(number) else { return nil }
                return (number, version)
            }
        guard let latest = releases.max(by: { $0.1 < $1.1 }) else { throw UpdateError.unavailable }
        guard latest.1 > current else { return nil }
        let prefix = "https://github.com/\(DesktopRelease.repository)/releases/download/desktop-v\(latest.0)/"
        let manifest = try await UpdateTransfer().fetch(URL(string: prefix + "update.json")!, limit: 64 * 1024)
        let signature = try await UpdateTransfer().fetch(URL(string: prefix + "update.json.sig")!, limit: 64)
        let release = try DesktopRelease.verify(manifest: manifest, signature: signature)
        guard release.version == latest.0 else { throw UpdateError.invalidManifest }
        return SignedDesktopRelease(release: release, manifest: manifest, signature: signature)
    }
}
