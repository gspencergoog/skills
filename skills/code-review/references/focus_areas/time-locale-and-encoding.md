# Time, Locale, and Encoding

Theme id: `time-locale-and-encoding`. Lane for merges: A.

## Cues (for triage)

- Construction, comparison, serialization, or arithmetic on dates, timestamps, time zones, or durations.
- String case conversion, sorting, number or currency formatting, or locale-sensitive parsing.
- Byte slicing, length checks, base64/hex conversions, or file/stream reads without an explicit encoding.

## Failure modes

1. Naive (zone-free) and timezone-aware timestamps, or local time and UTC, are mixed in comparisons, storage, or serialization.
   Look at: `now()`, `DateTime`, `Date`, or timestamp parsing calls and the time zone they assume. Settles it: trace the time zone attached at creation, storage, and comparison sites; `Read` or `Deduction`.
2. Adding 24 hours (or fixed seconds) in place of calendar day/month arithmetic produces wrong dates across DST transitions or month ends.
   Look at: duration math on wall-clock dates and month/year clamping logic. Settles it: trace the calculation on a DST spring-forward/fall-back day or on Jan 31 / Feb 29; `Deduction`.
3. Using wall-clock time instead of a monotonic clock to measure elapsed duration or timeouts misbehaves when the system clock jumps.
   Look at: `time.time()`, `Date.now()`, `DateTime.now()`, or `System.currentTimeMillis()` used for intervals. Settles it: check whether start and end timestamps come from a monotonic source; `Read`.
4. Case conversion, sorting, or number parsing uses the host's ambient locale instead of an invariant or explicit locale.
   Look at: `toLowerCase`, `toUpperCase`, `strftime`, `DecimalFormat`, or string collation on protocol identifiers or keys. Settles it: read the call and check whether an explicit locale or ASCII helper is passed; `Read`.
5. Code confuses byte length with UTF-8/UTF-16 code unit or grapheme length, truncating multi-byte characters mid-sequence.
   Look at: string slicing, buffer allocation, `len()` checks, or `Content-Length` calculations on non-ASCII text. Settles it: trace the slice or length operation on a multi-byte UTF-8 character; `Deduction`.
6. File or stream I/O omits an explicit character encoding and relies on the platform default (`locale.getpreferredencoding()`, Windows code pages).
   Look at: `open()`, `readAsString`, `InputStreamReader`, or subprocess pipes without `utf-8` specified. Settles it: read the I/O call signature and defaults; `Read`.
7. Strings compared or hashed as keys or identifiers are not Unicode-normalized first, so visually identical NFC and NFD forms fail to match.
   Look at: user-supplied or filename strings used in equality checks, map keys, or deduplication sets. Settles it: check whether inputs pass through NFC/NFKC normalization before comparison; `Read`.

## Hypothesis templates

- "Function `<fn>` compares a UTC timestamp from `<source_a>` with a local-time timestamp from `<source_b>`."
- "Slicing `<string_var>` at index `<n>` in `<fn>` splits a multi-byte UTF-8 sequence or surrogate pair."
- "Elapsed-time check in `<fn>` uses wall-clock `<clock_fn>`, which can go backward or jump on NTP adjustment."
- "File open call at `<file:line>` omits an explicit UTF-8 encoding and falls back to the platform default."
