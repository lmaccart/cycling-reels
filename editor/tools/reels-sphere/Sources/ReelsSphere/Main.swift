import Foundation

@main
struct ReelsSphere {
    static func main() async {
        do {
            let args = Array(CommandLine.arguments.dropFirst())
            guard args.count == 2, ["--check", "--render"].contains(args[0]) else {
                throw SphereFailure.invalid("usage: reels-sphere --check|--render /absolute/request.json")
            }
            let request: RenderRequest
            do { request = try JSONDecoder().decode(RenderRequest.self, from: Data(contentsOf: URL(fileURLWithPath: args[1]))) }
            catch { throw SphereFailure.invalid("request JSON: \(error)") }
            let renderer = try await OfflineRenderer(request)
            let result: [String: Any] = args[0] == "--check" ? ["supported": true, "renderer": "metal", "version": 1] : try renderer.render()
            let data = try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys])
            print(String(decoding: data, as: UTF8.self))
        } catch {
            FileHandle.standardError.write(Data("reels-sphere: \(error)\n".utf8))
            if case SphereFailure.unsupported = error { exit(3) }
            if case SphereFailure.invalid = error { exit(2) }
            exit(1)
        }
    }
}
