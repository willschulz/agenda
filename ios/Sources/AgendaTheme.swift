import SwiftUI
import UIKit

enum AgendaPalette {
    static let background = dynamic(light: 0xFBF9F4, dark: 0x161614)
    static let elevated = dynamic(light: 0xFFFFFF, dark: 0x1F1E1B)
    static let row = dynamic(light: 0xFAFAF7, dark: 0x1A1916)
    static let border = dynamic(light: 0xE3DFD5, dark: 0x34322D)
    static let softBorder = dynamic(light: 0xEDE9DF, dark: 0x2A2925)
    static let text = dynamic(light: 0x1C1B18, dark: 0xE9E6DD)
    static let muted = dynamic(light: 0x6B6964, dark: 0xA09C92)
    static let faint = dynamic(light: 0x9B988F, dark: 0x6B6862)
    static let accent = dynamic(light: 0x8A3324, dark: 0xD99A8B)
    static let blocker = dynamic(light: 0xC0473A, dark: 0xD4604F)
    static let inProgress = dynamic(light: 0x7C4DAA, dark: 0xB88DE0)
    static let flagged = dynamic(light: 0xC25A00, dark: 0xE07830)
    static let running = dynamic(light: 0x0A9999, dark: 0x3BBFBF)
    static let review = dynamic(light: 0xA07800, dark: 0xD4A020)

    private static func dynamic(light: Int, dark: Int) -> Color {
        Color(
            uiColor: UIColor { traits in
                UIColor(hex: traits.userInterfaceStyle == .dark ? dark : light)
            }
        )
    }
}

private extension UIColor {
    convenience init(hex: Int) {
        self.init(
            red: CGFloat((hex >> 16) & 0xFF) / 255,
            green: CGFloat((hex >> 8) & 0xFF) / 255,
            blue: CGFloat(hex & 0xFF) / 255,
            alpha: 1
        )
    }
}
