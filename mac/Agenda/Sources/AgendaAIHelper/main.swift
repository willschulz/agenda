import AgendaLocalAI
import Foundation

@main
struct AgendaAIHelper {
    static func main() async {
        do {
            let input = FileHandle.standardInput.readDataToEndOfFile()
            let request = try JSONDecoder().decode(LocalAIRequest.self, from: input)
            let response: LocalAIResponse
            switch request.operation {
            case "availability":
                response = LocalTitleGenerator.availability()
            case "title":
                guard let text = request.text?.trimmingCharacters(
                    in: .whitespacesAndNewlines
                ), !text.isEmpty else {
                    write(
                        LocalAIResponse(status: "error", reason: "missing-text"),
                        exitCode: 2
                    )
                    return
                }
                response = await LocalTitleGenerator.generate(
                    text: text,
                    prefix: request.prefix
                )
            case "insights":
                guard let mode = request.mode,
                      let context = request.context,
                      !context.isEmpty
                else {
                    write(
                        LocalAIResponse(
                            status: "error",
                            reason: "missing-insight-context"
                        ),
                        exitCode: 2
                    )
                    return
                }
                response = await LocalInsightGenerator.generate(
                    mode: mode,
                    context: context
                )
            default:
                write(
                    LocalAIResponse(status: "error", reason: "unknown-operation"),
                    exitCode: 2
                )
                return
            }
            write(response)
        } catch {
            write(
                LocalAIResponse(
                    status: "error",
                    reason: "invalid-request: \(error.localizedDescription)"
                ),
                exitCode: 2
            )
        }
    }

    private static func write(_ response: LocalAIResponse, exitCode: Int32 = 0) {
        do {
            let data = try JSONEncoder().encode(response)
            FileHandle.standardOutput.write(data)
            FileHandle.standardOutput.write(Data([0x0A]))
        } catch {
            FileHandle.standardError.write(
                Data("could not encode response\n".utf8)
            )
            exit(2)
        }
        if exitCode != 0 { exit(exitCode) }
    }
}
