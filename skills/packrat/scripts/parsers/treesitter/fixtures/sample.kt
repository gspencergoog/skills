/** Global timeout in milliseconds. */
private val DEFAULT_TIMEOUT_MS = 5000

/** String map shorthand. */
typealias Headers = Map<String, String>

/** Execution modes for sync tasks. */
enum class SyncMode {
  FAST,
  FULL
}

/** Storage repository contract. */
interface Repository {
  fun count(): Int
}

/** Process-wide registry singleton. */
object Registry {
  val version: Int = 1
}

/**
 * Immutable user account model.
 */
public data class UserAccount(val id: Int, private var handle: String, transientToken: String) : Repository {
  val secondaryLabel: Int
    get() = id

  /** Secondary constructor from id only. */
  constructor(id: Int) : this(id, "anon", "")

  internal var nickname: String = handle
    get() = field
    set(value) { field = value }

  /** Returns item count. */
  override fun count(): Int {
    fun nestedLocal(): Int = 1
    return nestedLocal()
  }

  protected fun clearHandle() {
    handle = ""
  }

  companion object Factory {
    /** Builds a guest account. */
    fun guest(): UserAccount = UserAccount(0)
  }
}

/** Uppercases a receiver string with exclamation. */
fun String.shout(): String = this.uppercase()
