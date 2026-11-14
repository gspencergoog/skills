/// Default limit for widget queries.
public let defaultLimit: Int = 100
private var secretCounter: Int = 0

/// Callback signature for widget events.
public typealias WidgetHandler = (String) -> Void

/// Supported widget colors.
public enum Color: String {
    /// Bright red variant.
    case red = "red"
    case green = "green"
}

/// Contract for renderable UI elements.
public protocol Renderable {
    func render() -> String
}

/**
 * Value-type layout configuration.
 */
internal struct LayoutConfig {
    public var width: Int
    package var height: Int

    public init(width: Int, height: Int) {
        self.width = width
        self.height = height
    }
}

/// Thread-safe telemetry collector.
public actor TelemetryActor {
    public func record(event: String) {}
}

/// Base UI widget with lifecycle hooks.
open class BaseWidget: Renderable {
    public var count: Int = 0
    fileprivate var internalTag: String = "tag"

    /// Initialize the widget with a label.
    public init(label: String) {
        self._label = label
    }

    private var _label: String

    /// Current widget label.
    public var label: String {
        get { return _label }
        set { _label = newValue }
    }

    public var sameLineAccessor: Int { get { count } set { count = newValue } }

    /// Access child item by index.
    public subscript(index: Int) -> String {
        return _label
    }

    /// Render the widget description.
    open func render() -> String {
        func localHelper() -> String {
            return "ignored"
        }
        let closure = { () -> Int in
            return 42
        }
        _ = closure()
        return localHelper()
    }
}

extension String {
    /// Uppercased string with exclamation mark.
    public func shout() -> String {
        return self.uppercased() + "!"
    }
}

/// Render every widget and return total count.
@MainActor
public func renderAll(
    items: [BaseWidget],
    strict: Bool = false
) -> Int {
    func nestedLocal() {}
    nestedLocal()
    return items.count
}
