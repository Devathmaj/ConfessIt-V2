import type { ReactNode } from "react";

export type DocumentKey = "about" | "privacy" | "terms" | "disclaimer";

const DOCUMENT_LINKS: Record<string, DocumentKey> = {
  "About.md": "about",
  "Privacy-Policy.md": "privacy",
  "Terms-of-Service.md": "terms",
  "Disclaimer.md": "disclaimer",
};

const INLINE_PATTERN = /(\*\*[^*]+\*\*|\[[^\]]+\]\([^)]+\)|`[^`]+`)/g;

function documentKeyForHref(href: string): DocumentKey | null {
  const cleaned = href.replace(/^\.\//, "").split("#")[0];
  return DOCUMENT_LINKS[cleaned] ?? null;
}

function renderInline(
  text: string,
  onDocumentLink: (key: DocumentKey) => void,
): ReactNode[] {
  const nodes: ReactNode[] = [];
  let lastIndex = 0;
  let match: RegExpExecArray | null;

  INLINE_PATTERN.lastIndex = 0;
  while ((match = INLINE_PATTERN.exec(text)) !== null) {
    if (match.index > lastIndex) {
      nodes.push(text.slice(lastIndex, match.index));
    }

    const token = match[0];
    const key = `inline-${match.index}`;

    if (token.startsWith("**")) {
      nodes.push(
        <strong key={key} className="font-semibold text-foreground">
          {token.slice(2, -2)}
        </strong>,
      );
    } else if (token.startsWith("`")) {
      nodes.push(
        <code key={key} className="break-all rounded bg-muted px-1 py-0.5 font-mono text-[0.9em]">
          {token.slice(1, -1)}
        </code>,
      );
    } else {
      const linkMatch = /^\[([^\]]+)\]\(([^)]+)\)$/.exec(token);
      if (!linkMatch) {
        nodes.push(token);
      } else {
        const [, label, href] = linkMatch;
        const documentKey = documentKeyForHref(href);
        const linkClass =
          "break-words text-romantic underline underline-offset-2 transition-opacity hover:opacity-80 [overflow-wrap:anywhere]";

        if (documentKey) {
          nodes.push(
            <button
              key={key}
              type="button"
              onClick={() => onDocumentLink(documentKey)}
              className={`${linkClass} text-left`}
            >
              {label}
            </button>,
          );
        } else {
          const external = /^https?:/i.test(href);
          nodes.push(
            <a
              key={key}
              href={href}
              className={linkClass}
              {...(external ? { target: "_blank", rel: "noopener noreferrer" } : {})}
            >
              {label}
            </a>,
          );
        }
      }
    }

    lastIndex = match.index + token.length;
  }

  if (lastIndex < text.length) {
    nodes.push(text.slice(lastIndex));
  }

  return nodes;
}

interface LegalDocumentProps {
  markdown: string;
  onDocumentLink: (key: DocumentKey) => void;
}

export function LegalDocument({ markdown, onDocumentLink }: LegalDocumentProps) {
  const lines = markdown.replace(/\r\n/g, "\n").split("\n");
  const blocks: ReactNode[] = [];
  let index = 0;
  let blockKey = 0;

  while (index < lines.length) {
    const line = lines[index];

    if (!line.trim()) {
      index += 1;
      continue;
    }

    const heading = /^(#{1,6})\s+(.+)$/.exec(line);
    if (heading) {
      const level = heading[1].length;
      const content = renderInline(heading[2].trim(), onDocumentLink);
      const key = `heading-${blockKey++}`;

      if (level === 1) {
        blocks.push(
          <h2 key={key} className="mb-4 mt-2 break-words font-dancing text-3xl text-romantic">
            {content}
          </h2>,
        );
      } else if (level === 2) {
        blocks.push(
          <h3 key={key} className="mb-2 mt-7 break-words text-xl font-semibold text-foreground">
            {content}
          </h3>,
        );
      } else {
        blocks.push(
          <h4 key={key} className="mb-2 mt-5 break-words text-lg font-semibold text-foreground">
            {content}
          </h4>,
        );
      }

      index += 1;
      continue;
    }

    if (/^[-*]\s+/.test(line)) {
      const items: ReactNode[] = [];
      let itemIndex = 0;

      while (index < lines.length && /^[-*]\s+/.test(lines[index])) {
        const item = lines[index].replace(/^[-*]\s+/, "");
        items.push(
          <li key={`item-${itemIndex++}`} className="break-words pl-1">
            {renderInline(item, onDocumentLink)}
          </li>,
        );
        index += 1;
      }

      blocks.push(
        <ul key={`list-${blockKey++}`} className="mb-4 min-w-0 list-disc space-y-2 pl-6">
          {items}
        </ul>,
      );
      continue;
    }

    const paragraph: ReactNode[] = [];
    let paragraphIndex = 0;

    while (
      index < lines.length &&
      lines[index].trim() &&
      !/^(#{1,6})\s+/.test(lines[index]) &&
      !/^[-*]\s+/.test(lines[index])
    ) {
      const text = lines[index].trim();

      if (paragraph.length > 0) {
        const previousHardBreak = / {2,}$/.test(lines[index - 1] ?? "");
        paragraph.push(
          previousHardBreak ? <br key={`br-${paragraphIndex}`} /> : " ",
        );
      }

      paragraph.push(...renderInline(text, onDocumentLink));
      paragraphIndex += 1;
      index += 1;
    }

    blocks.push(
      <p key={`paragraph-${blockKey++}`} className="mb-4 break-words leading-relaxed">
        {paragraph}
      </p>,
    );
  }

  return (
    <div className="min-w-0 max-w-full break-words text-muted-foreground [overflow-wrap:anywhere]">
      {blocks}
    </div>
  );
}
