/// Default maximum item count.
const int defaultLimit = 100;

bool _internalFlag = false;

/// JSON dictionary payload.
typedef JsonMap = Map<String, Object?>;

typedef bool ItemPredicate<T>(T item);

/**
 * Supported badge severities.
 *
 * Ordered from lowest to highest.
 */
enum Severity {
  /// Informational badge.
  info,
  warning,
}

/// Tracks disposal state for lifecycle objects.
base mixin DisposableMixin {
  bool _disposed = false;

  void dispose() {
    _disposed = true;
  }
}

/// Interactive card widget with title and counter.
@Deprecated('Use ModernCard instead')
class WidgetCard with DisposableMixin {
  /// Card heading text.
  final String title;
  int _counter = 0;

  /// Creates a standard card.
  WidgetCard(this.title);

  /** Creates a compact card preset. */
  WidgetCard.compact() : title = 'compact';

  WidgetCard._internal(this.title, this._counter);

  int get counter => _counter; set counter(int value) { _counter = value; }

  /// Renders the card summary string.
  @override
  String render({bool uppercase = false}) {
    String localFormatter(String input) {
      return uppercase ? input.toUpperCase() : input;
    }

    final mapper = (String s) => localFormatter(s);
    return mapper(title);
  }

  WidgetCard operator +(WidgetCard other) =>
      WidgetCard('$title+${other.title}');
}

/// String trimming utilities.
extension StringTrimExt on String {
  String collapseTrim() => trim();
}

extension on List<int> {
  int get firstOrZero => isEmpty ? 0 : first;
}

/// Strongly typed entity identifier.
extension type const EntityId(int raw) implements int {
  bool get isPositive => raw > 0;
}

int _globalSeed = 1;

int get globalSeed => _globalSeed;

set globalSeed(int next) {
  _globalSeed = next;
}

/// Computes total rendered length across [cards].
int summarizeCards(
  List<WidgetCard> cards, {
  bool uppercase = false,
}) {
  int nestedLocal() => cards.length;
  return nestedLocal();
}

void _privateHelper() {}
