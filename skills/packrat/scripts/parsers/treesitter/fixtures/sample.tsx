/** Props passed to a badge component. */
export interface BadgeProps {
  text: string;
}

/** Renders an inline status badge. */
export function Badge(props: BadgeProps) {
  const localFormat = (s: string) => s.trim();
  return <span className="badge">{localFormat(props.text)}</span>;
}

/** Stateful card component. */
export class Card {
  title = "Card";

  renderView() {
    return <section><h1>{this.title}</h1></section>;
  }
}
