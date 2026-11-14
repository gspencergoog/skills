class BrokenKotlin(val x: Int {
  fun unclosed(: String {
    return "broken"
  }
}

fun recoveredFn(): Int = 1
