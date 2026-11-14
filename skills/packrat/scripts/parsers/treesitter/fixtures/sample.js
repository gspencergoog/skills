/** Default retry count for requests. */
export const MAX_RETRIES = 3;
let moduleCache = null;

/**
 * Counter widget written in plain JavaScript.
 */
export class Counter {
  count = 0;
  #step = 1;

  constructor(initial = 0) {
    this.count = initial;
  }

  get current() { return this.count; } set current(v) { this.count = v; }

  /** Increments the counter by step. */
  increment() {
    function hiddenHelper() {
      return 1;
    }
    this.count += this.#step * hiddenHelper();
    return this.count;
  }
}

/// Creates a default counter instance.
export function createCounter() {
  return new Counter(0);
}
