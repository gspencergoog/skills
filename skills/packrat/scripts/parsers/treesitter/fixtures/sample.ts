/** Top-level limit configuration. */
export const DEFAULT_LIMIT = 100;
const internalCounter = 0;

/// Alias for identifiers.
export type WidgetId = string | number;

/** Supported display themes. */
export enum Theme {
  Light = "light",
  Dark = "dark",
}

/** Contract for renderable components. */
export interface Renderable {
  readonly id: WidgetId;
  render(): string;
}

/** Namespace for layout utilities. */
export namespace Layout {
  /** Measures available width. */
  export function measure(px: number): number {
    return px * 2;
  }
}

/**
 * Base UI widget supporting overloads and private state.
 */
@Sealed()
export class Widget implements Renderable {
  /** Public identifier. */
  public readonly id: WidgetId;
  protected label: string = "default";
  #secretToken = "xyz";

  /** Creates a widget instance. */
  constructor(id: WidgetId) {
    this.id = id;
  }

  get title(): string { return this.label; } set title(v: string) { this.label = v; }

  /** Formats widget output. */
  format(prefix: string): string;
  format(prefix: string, uppercase: boolean): string;
  format(prefix: string, uppercase = false): string {
    function localHelper(): string {
      return prefix;
    }
    const arrowLocal = () => localHelper();
    return uppercase ? arrowLocal().toUpperCase() : arrowLocal();
  }

  private reset(): void {
    this.label = "";
  }
}

/*! Parses raw widget input. */
export function parseWidget(raw: string): Widget {
  return new Widget(raw);
}
