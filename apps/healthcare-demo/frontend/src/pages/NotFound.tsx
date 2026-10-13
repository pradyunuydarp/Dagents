import { href } from "../router";

export function NotFound({ what }: { what?: string }) {
  return (
    <div className="page">
      <header className="page-head">
        <p className="ds-eyebrow">Not found</p>
        <h1>Page not found</h1>
        <p className="lede ds-measure">{what ?? "This page does not exist."}</p>
        <p>
          <a href={href("")}>Go to the consortium page</a>
        </p>
      </header>
    </div>
  );
}
